"""MODIS MCDWD flood detection fetcher.

Provides flood detection from the NASA MODIS MCDWD product family at
~250 m resolution. Two backends:

- ``lance_geotiff`` (default) — LANCE single-composite GeoTIFFs,
  streamable via ``/vsicurl/``, ~1-week NRT window.
- ``laads_hdf4`` — LAADS HDF4 archive (reprocessed 2003–2025 + archived
  NRT 2026 onward), download-only.

Both require ``EARTHDATA_TOKEN``; see :mod:`atlantis.fetchers.modis.backend`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import rasterio
import requests
from loguru import logger
from shapely.geometry import box

from atlantis.config import get_config
from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult, SearchResult
from atlantis.fetchers.modis.backend import (
    LANCE_RETENTION_DAYS,
    MissingEarthdataTokenError,
    earthdata_auth_headers,
    get_backend,
    get_earthdata_token,
    list_backends,
)
from atlantis.fetchers.modis.dataset import processed_tile_to_dataset
from atlantis.fetchers.modis.processor import (
    COMPOSITE_TO_HDF_LAYER,
    ModisRasterProcessor,
    OutputPaths,
    ProcessTilesResult,
    modis_tiles_for_bbox,
    tile_bounds_from_hv,
)
from atlantis.fetchers.modis.selection import flood_pixel_count, select_peak_window, subsample_around_peak
from atlantis.fetchers.registry import register_fetcher
from atlantis.models.event import FloodEvent
from atlantis.utils.io import download_file, ensure_dir

if TYPE_CHECKING:
    import xarray as xr


DEFAULT_LANCE_PRIMARY_BASE_URL = "https://nrt3.modaps.eosdis.nasa.gov"
DEFAULT_LANCE_BACKUP_BASE_URL = "https://nrt4.modaps.eosdis.nasa.gov"
DEFAULT_LAADS_BASE_URL = "https://ladsweb.modaps.eosdis.nasa.gov"

VALID_COMPOSITES: set[str] = set(COMPOSITE_TO_HDF_LAYER.keys())
VALID_STRATEGIES: set[str] = {"peak", "aggregate", "all"}


@dataclass
class ModisSearchDiagnostics:
    """Structured explanation of why a search returned a given result set.

    Mirrors :class:`atlantis.fetchers.viirs.SearchDiagnostics` with two
    MODIS-specific fields (``auth_token_missing``, ``outside_lance_window``).
    """

    backend: str
    composite: str
    tile_count: int = 0
    requested_years: set[int] = field(default_factory=set)
    available_years: set[int] | None = None
    skipped_years: set[int] = field(default_factory=set)
    dates_probed: int = 0
    dates_with_listings: int = 0
    dates_with_matches: int = 0
    result_count: int = 0
    network_failures: int = 0
    last_network_error: str | None = None
    auth_token_missing: bool = False
    outside_lance_window: bool = False

    @property
    def year_coverage_gap(self) -> bool:
        """True when every requested year is known to be unpublished."""
        return bool(self.skipped_years) and self.skipped_years == self.requested_years

    @property
    def listings_all_empty(self) -> bool:
        """True when listings were attempted but none returned entries."""
        return self.dates_probed > 0 and self.dates_with_listings == 0

    @property
    def no_tile_match_in_listings(self) -> bool:
        """True when listings returned entries but no (h, v) tile matched."""
        return self.dates_with_listings > 0 and self.dates_with_matches == 0

    @property
    def network_unreachable(self) -> bool:
        """True when every probed date failed due to a network/HTTP error."""
        return self.dates_probed > 0 and self.network_failures == self.dates_probed


def _date_range(start: datetime, end: datetime) -> list[datetime]:
    dates: list[datetime] = []
    current = start
    while current <= end:
        dates.append(current)
        current += timedelta(days=1)
    return dates


def _normalise_backend(value: str) -> str:
    backend = value.strip().lower()
    supported = set(list_backends())
    if backend not in supported:
        supported_str = ", ".join(sorted(supported))
        raise ValueError(f"Unsupported MODIS backend '{value}'. Expected one of: {supported_str}")
    return backend


def _normalise_composite(value: str) -> str:
    composite = value.strip().upper()
    if composite not in VALID_COMPOSITES:
        supported = ", ".join(sorted(VALID_COMPOSITES))
        raise ValueError(f"Unsupported MODIS composite '{value}'. Expected one of: {supported}")
    return composite


@register_fetcher("modis")
class MODISFetcher(AbstractFloodFetcher):
    """Fetcher for MODIS MCDWD flood detection data."""

    source_id: str = "modis"

    def __init__(
        self,
        backend: str | None = None,
        composite: str | None = None,
        base_url: str | None = None,
        backup_base_url: str | None = None,
        timeout: int | None = None,
        classify: bool = False,
        stream: bool = False,
        strategy: str = "peak",
        keep_processed: bool = True,
        peak_days_before: int = 0,
        peak_days_after: int = 0,
        max_observations: int = 0,
        peak_priority: str = "post",
    ) -> None:
        """Initialise the MODIS fetcher.

        Args:
            backend: ``"lance_geotiff"`` (streamable) or ``"laads_hdf4"``
                (download).
            composite: One of ``"F1"``, ``"F1C"``, ``"F2"``, ``"F3"``.
            base_url: Optional override for the backend's base URL. When
                omitted, sensible defaults are picked per backend.
            backup_base_url: Optional ``nrt4`` mirror URL for the
                ``lance_geotiff`` backend.
            timeout: HTTP request timeout in seconds.
            classify: Decode raw codes into ``flood_fraction``,
                ``recurring_flood``, ``permanent_water``, ``quality_mask``.
            stream: Stream remote tiles via GDAL ``/vsicurl/``. Only valid
                for ``lance_geotiff``; raises ``ValueError`` otherwise.
            strategy: Multi-date reduction (``peak`` / ``aggregate`` / ``all``).
            keep_processed: When True, write intermediate processed/ GeoTIFFs.
            peak_days_before: Days before the computed peak to include when
                filtering dates. 0 means no window filtering. Requires
                *peak_days_after* > 0 to have any effect (or vice-versa).
            peak_days_after: Days after the computed peak to include.
            max_observations: Maximum number of dates to return after windowing.
                0 means no limit. Selection order is controlled by
                *peak_priority*.
            peak_priority: How to fill *max_observations* around the peak:
                ``"post"`` (post-event first, then pre), ``"pre"`` (pre-event
                first, then post), or ``"balanced"`` (alternating ±1, ±2, …).
        """
        config = get_config()
        fc = config.fetcher

        self.backend_name = _normalise_backend(backend or fc.modis_backend)
        self.composite = _normalise_composite(composite or fc.modis_composite)

        if self.backend_name == "lance_geotiff":
            self.base_url = (base_url or fc.modis_lance_primary_base_url or DEFAULT_LANCE_PRIMARY_BASE_URL).rstrip("/")
            self.backup_base_url = backup_base_url or fc.modis_lance_backup_base_url or DEFAULT_LANCE_BACKUP_BASE_URL
            self.backup_base_url = self.backup_base_url.rstrip("/") if self.backup_base_url else None
            self.backend = get_backend(self.backend_name, backup_base_url=self.backup_base_url)
        else:
            self.base_url = (base_url or fc.modis_laads_base_url or DEFAULT_LAADS_BASE_URL).rstrip("/")
            self.backup_base_url = None
            self.backend = get_backend(self.backend_name)

        self.timeout = timeout or fc.timeout
        self.classify = classify
        self.stream = stream
        self.strategy = strategy
        self.keep_processed = keep_processed
        self.last_diagnostics: ModisSearchDiagnostics | None = None

        if self.strategy not in VALID_STRATEGIES:
            raise ValueError(f"Invalid strategy '{self.strategy}'. Expected one of: {sorted(VALID_STRATEGIES)}")
        if self.stream and not self.backend.supports_streaming:
            raise ValueError(
                f"Backend '{self.backend_name}' does not support --stream. "
                "Use --no-stream or switch to --modis-backend lance_geotiff."
            )

        if peak_days_before < 0:
            raise ValueError(f"peak_days_before must be non-negative, got {peak_days_before}")
        if peak_days_after < 0:
            raise ValueError(f"peak_days_after must be non-negative, got {peak_days_after}")
        if max_observations < 0:
            raise ValueError(f"max_observations must be non-negative, got {max_observations}")
        if peak_priority not in {"post", "pre", "balanced"}:
            raise ValueError(f"Invalid peak_priority '{peak_priority}'. Expected 'post', 'pre', or 'balanced'.")

        self.peak_days_before = peak_days_before
        self.peak_days_after = peak_days_after
        self.max_observations = max_observations
        self.peak_priority = peak_priority

    # ── Helpers ─────────────────────────────────────────────────────────

    def _event_dates(self, event: FloodEvent) -> list[datetime]:
        start = datetime.combine(event.start_date, time.min, tzinfo=timezone.utc)
        end = datetime.combine(event.end_date, time.min, tzinfo=timezone.utc)
        return _date_range(start, end)

    def _intersecting_tiles(self, event: FloodEvent) -> list[tuple[int, int]]:
        return modis_tiles_for_bbox(event.bbox)

    def _is_within_lance_window(self, event_date: datetime) -> bool:
        cutoff = datetime.now(timezone.utc) - timedelta(days=LANCE_RETENTION_DAYS)
        return event_date >= cutoff

    # ── Search ──────────────────────────────────────────────────────────

    def search(self, event: FloodEvent) -> list[SearchResult]:
        """Discover MCDWD tiles intersecting the event bbox + date range.

        Populates :attr:`last_diagnostics` with structured information
        explaining empty result sets (auth, coverage gap, LANCE window,
        listing/match counts) so the CLI can surface actionable hints.
        """
        event_dates = self._event_dates(event)
        requested_years = {dt.year for dt in event_dates}
        diagnostics = ModisSearchDiagnostics(
            backend=self.backend_name,
            composite=self.composite,
            requested_years=requested_years,
        )
        self.last_diagnostics = diagnostics

        try:
            tiles = self._intersecting_tiles(event)
        except ValueError as exc:
            logger.warning("MODIS search aborted: {}", exc)
            return []
        diagnostics.tile_count = len(tiles)
        if not tiles:
            logger.warning("MODIS search: bbox {} maps to zero tiles", event.bbox)
            return []

        # Auth check — reused on every listing call but cheaper to fail fast here.
        try:
            headers = earthdata_auth_headers()
        except MissingEarthdataTokenError as exc:
            diagnostics.auth_token_missing = True
            logger.warning("MODIS search: {}", exc)
            return []

        available_years = self.backend.available_years(self.base_url, self.timeout)
        diagnostics.available_years = available_years
        if available_years is not None:
            diagnostics.skipped_years = requested_years - available_years
            if diagnostics.skipped_years:
                logger.warning(
                    "MODIS backend '{}' does not publish year(s) {}; published years: {}",
                    self.backend_name,
                    sorted(diagnostics.skipped_years),
                    sorted(available_years),
                )

        results: list[SearchResult] = []
        outside_window_dates = 0

        for event_date in event_dates:
            if available_years is not None and event_date.year not in available_years:
                continue
            if self.backend_name == "lance_geotiff" and not self._is_within_lance_window(event_date):
                outside_window_dates += 1
                continue

            diagnostics.dates_probed += 1
            location = self.backend.get_listing_location(self.base_url, event_date, self.composite)
            try:
                entries = self.backend.get_directory_listing(self.base_url, location, self.timeout, headers=headers)
            except requests.RequestException as exc:
                diagnostics.network_failures += 1
                diagnostics.last_network_error = str(exc)
                logger.warning(
                    "MODIS backend '{}' network error while listing {}: {}",
                    self.backend_name,
                    location.locator,
                    exc,
                )
                continue
            if not entries:
                logger.debug("Date {}: no entries in listing", location.date_token)
                continue
            diagnostics.dates_with_listings += 1

            date_match_count = 0
            for h, v in tiles:
                entry = self.backend.find_remote_filename(h, v, self.composite, entries)
                if entry is None:
                    continue
                date_match_count += 1
                west, south, east, north = tile_bounds_from_hv(h, v)
                results.append(
                    SearchResult(
                        source_id=self.source_id,
                        item_id=f"modis:{location.date_token}:h{h:02d}v{v:02d}",
                        timestamp=event_date,
                        bbox=(west, south, east, north),
                        url=self.backend.build_result_url(self.base_url, location, entry),
                        properties={
                            "h": h,
                            "v": v,
                            "date": location.date_token,
                            "filename": entry.filename,
                            "prod_timestamp": entry.prod_timestamp,
                            "backend": self.backend_name,
                            "composite": self.composite,
                        },
                    )
                )
            if date_match_count > 0:
                diagnostics.dates_with_matches += 1
            logger.debug(
                "Date {}: {} entries, {} tile matches",
                location.date_token,
                len(entries),
                date_match_count,
            )

        if self.backend_name == "lance_geotiff" and not results and outside_window_dates > 0:
            diagnostics.outside_lance_window = True

        diagnostics.result_count = len(results)
        return results

    # ── Fetch ───────────────────────────────────────────────────────────

    def fetch(self, event: FloodEvent, output_dir: Path) -> list[FetchResult]:
        """Fetch MCDWD data for ``event``, materialising tiles + processing.

        Streams (``stream=True`` + ``lance_geotiff``) or downloads tiles
        with bearer auth, then mosaic/clip/classify and dispatch the
        configured strategy (``peak`` / ``aggregate`` / ``all``).
        """
        search_results = self.search(event)
        if not search_results:
            return []

        output_dir = ensure_dir(output_dir)
        raw_dir = ensure_dir(output_dir / "raw") if not self.stream else output_dir / "raw"
        processed_dir = output_dir / "processed"
        if self.keep_processed:
            processed_dir = ensure_dir(processed_dir)

        grouped: dict[str, list[SearchResult]] = defaultdict(list)
        for result in search_results:
            grouped[result.properties["date"]].append(result)

        area_geom = box(*event.bbox)
        processor = ModisRasterProcessor(area_geom, classify=self.classify, composite=self.composite)

        all_processed: list[tuple[str, ProcessTilesResult]] = []

        # Inject the bearer header into GDAL only when actually streaming.
        gdal_env: dict[str, str] = {}
        if self.stream:
            try:
                token = get_earthdata_token()
            except MissingEarthdataTokenError as exc:
                logger.error("MODIS fetch (stream): {}", exc)
                return []
            gdal_env["GDAL_HTTP_HEADERS"] = f"Authorization: Bearer {token}"

        for date_token, dated_results in sorted(grouped.items()):
            mode_label = "stream" if self.stream else "download"
            logger.debug(
                "Processing date {}: {} tile(s) ({} mode)",
                date_token,
                len(dated_results),
                mode_label,
            )

            if self.stream:
                tile_sources: list[Path | str] = [
                    r.url
                    for r in sorted(
                        dated_results,
                        key=lambda item: (item.properties["h"], item.properties["v"]),
                    )
                ]
                if not tile_sources:
                    continue
                with rasterio.Env(**gdal_env):
                    process_result = processor.process_tiles(
                        tile_sources,
                        event.event_id,
                        date_token,
                        processed_dir,
                        write_outputs=self.keep_processed,
                    )
            else:
                tile_paths_local: list[Path | str] = []
                try:
                    headers = earthdata_auth_headers()
                except MissingEarthdataTokenError as exc:
                    logger.error("MODIS fetch (download): {}", exc)
                    return []
                for r in sorted(
                    dated_results,
                    key=lambda item: (item.properties["h"], item.properties["v"]),
                ):
                    filename = r.properties["filename"]
                    download_path = raw_dir / filename
                    download_file(r.url, output_path=download_path, headers=headers)
                    tile_paths_local.append(download_path)
                if not tile_paths_local:
                    continue
                process_result = processor.process_tiles(
                    tile_paths_local,
                    event.event_id,
                    date_token,
                    processed_dir,
                    write_outputs=self.keep_processed,
                )

            if process_result is not None:
                all_processed.append((date_token, process_result))

        if not all_processed:
            return []

        all_processed = self._apply_peak_window(all_processed)
        if not all_processed:
            return []

        return self._dispatch_strategy(event.event_id, all_processed)

    # ── Peak-window filter / subsampling ────────────────────────────────

    def _apply_peak_window(
        self,
        all_processed: list[tuple[str, ProcessTilesResult]],
    ) -> list[tuple[str, ProcessTilesResult]]:
        """Apply peak-window filter and max-observations subsampling.

        No-op when neither *peak_days_before/after* nor *max_observations*
        is set. Mirrors :meth:`atlantis.fetchers.viirs.VIIRSFetcher._apply_peak_window`.
        """
        uses_window = self.peak_days_before > 0 or self.peak_days_after > 0
        uses_subsample = self.max_observations > 0

        if not uses_window and not uses_subsample:
            return all_processed

        date_tokens = [dt for dt, _ in all_processed]
        processed_map = {dt: pr.processed for dt, pr in all_processed}

        if uses_window:
            surviving = select_peak_window(
                date_tokens,
                processed_map,
                days_before=self.peak_days_before,
                days_after=self.peak_days_after,
            )
            if surviving:
                peak_token = max(surviving, key=lambda t: flood_pixel_count(processed_map[t]))
            else:
                peak_token = None
            logger.debug(
                "MODIS peak-window [-{}, +{}]: {} → {} date(s) (peak={})",
                self.peak_days_before,
                self.peak_days_after,
                len(date_tokens),
                len(surviving),
                peak_token,
            )
        else:
            surviving = list(date_tokens)
            peak_token = max(surviving, key=lambda t: flood_pixel_count(processed_map[t])) if surviving else None

        if uses_subsample and peak_token is not None and len(surviving) > self.max_observations:
            surviving = subsample_around_peak(
                surviving,
                peak_token,
                self.max_observations,
                self.peak_priority,
            )
            logger.debug(
                "MODIS subsampled to {} observation(s) (priority='{}', peak={})",
                len(surviving),
                self.peak_priority,
                peak_token,
            )

        surviving_set = set(surviving)
        return [(dt, pr) for dt, pr in all_processed if dt in surviving_set]

    # ── Strategy dispatch ───────────────────────────────────────────────

    def _dispatch_strategy(
        self,
        event_id: str,
        all_processed: list[tuple[str, ProcessTilesResult]],
    ) -> list[FetchResult]:
        logger.debug(
            "Strategy '{}': {} date(s) processed successfully",
            self.strategy,
            len(all_processed),
        )

        if self.strategy == "all":
            if self.keep_processed:
                return [self._fetch_result_from_process(event_id, dt, pr) for dt, pr in all_processed]
            return [self._in_memory_fetch_result(event_id, dt, pr) for dt, pr in all_processed]

        if self.strategy == "aggregate":
            tiles = [pr.processed for _, pr in all_processed]
            aggregated = ModisRasterProcessor.aggregate_tiles(tiles)
            return [
                self._in_memory_fetch_result(
                    event_id,
                    "aggregated",
                    ProcessTilesResult(OutputPaths(), all_processed[0][1].metadata, aggregated),
                )
            ]

        # peak
        best_date_token, best_result = max(all_processed, key=lambda item: flood_pixel_count(item[1].processed))
        logger.debug(
            "Peak date selected: {} ({} flood pixels)",
            best_date_token,
            flood_pixel_count(best_result.processed),
        )
        if self.keep_processed:
            return [self._fetch_result_from_process(event_id, best_date_token, best_result)]
        return [self._in_memory_fetch_result(event_id, best_date_token, best_result)]

    @staticmethod
    def _fetch_result_from_process(event_id: str, date_token: str, process_result: ProcessTilesResult) -> FetchResult:
        paths = process_result.paths
        files = [
            p
            for p in (
                paths.raw,
                paths.flood_fraction,
                paths.quality_mask,
                paths.permanent_water,
                paths.recurring_flood,
            )
            if p is not None
        ]
        return FetchResult(
            event_id=event_id,
            source_id="modis",
            files=files,
            metadata=process_result.metadata,
            date_token=date_token,
        )

    def _in_memory_fetch_result(
        self,
        event_id: str,
        date_token: str,
        process_result: ProcessTilesResult,
    ) -> FetchResult:
        dataset = processed_tile_to_dataset(
            process_result.processed,
            event_id=event_id,
            source_id=self.source_id,
        )
        return FetchResult(
            event_id=event_id,
            source_id=self.source_id,
            files=[],
            metadata=process_result.metadata,
            date_token=date_token,
            dataset=dataset,
        )

    # ── to_dataset ──────────────────────────────────────────────────────

    def to_dataset(self, result: FetchResult) -> "xr.Dataset":
        """Convert a :class:`FetchResult` to a georeferenced xarray Dataset.

        Returns the in-memory dataset attached to ``result`` when one is
        present (``--no-keep-processed`` mode); otherwise reads the
        per-layer GeoTIFFs back from disk via rioxarray.
        """
        try:
            import rioxarray as rxr
            import xarray as xr
        except ImportError as exc:
            raise ImportError("rioxarray and xarray are required to read MODIS datasets") from exc

        if result.dataset is not None:
            return result.dataset

        files_by_name = {path.name: path for path in result.files}

        raw_path = next((path for name, path in files_by_name.items() if name.endswith("_raw.tif")), None)

        variables: dict = {}
        if raw_path:
            variables["raw"] = rxr.open_rasterio(raw_path).squeeze(drop=True).rename("raw")
        else:
            ff_path = next(path for name, path in files_by_name.items() if name.endswith("_flood_fraction.tif"))
            qm_path = next(path for name, path in files_by_name.items() if name.endswith("_quality_mask.tif"))
            pw_path = next(path for name, path in files_by_name.items() if name.endswith("_permanent_water.tif"))
            rf_path = next(
                (path for name, path in files_by_name.items() if name.endswith("_recurring_flood.tif")),
                None,
            )

            variables["flood_fraction"] = (
                rxr.open_rasterio(ff_path).squeeze(drop=True).astype("float32") / 100.0
            ).rename("flood_fraction")
            variables["quality_mask"] = (
                rxr.open_rasterio(qm_path).squeeze(drop=True).astype("uint8").rename("quality_mask")
            )
            variables["permanent_water"] = (
                rxr.open_rasterio(pw_path).squeeze(drop=True).astype("uint8").rename("permanent_water")
            )
            if rf_path is not None:
                variables["recurring_flood"] = (
                    rxr.open_rasterio(rf_path).squeeze(drop=True).astype("uint8").rename("recurring_flood")
                )

        dataset = xr.Dataset(variables)
        dataset.attrs["source_id"] = self.source_id
        dataset.attrs["event_id"] = result.event_id
        return dataset
