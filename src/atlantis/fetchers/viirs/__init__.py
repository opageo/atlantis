"""VIIRS flood detection fetcher.

VIIRS provides flood detection from Suomi-NPP and NOAA-20 satellites.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zipfile import ZipFile

import geopandas as gpd
import requests
from loguru import logger
from shapely.geometry import box

from atlantis.config import get_config
from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult, SearchResult
from atlantis.fetchers.registry import register_fetcher
from atlantis.fetchers.viirs.backend import get_backend, list_backends
from atlantis.fetchers.viirs.dataset import processed_tile_to_dataset
from atlantis.fetchers.viirs.processor import (
    CLASSIFIED_FLOOD_NODATA,
    OutputPaths,
    ProcessTilesResult,
    ViirsRasterProcessor,
)
from atlantis.fetchers.viirs.selection import flood_pixel_count, select_peak_window, subsample_around_peak
from atlantis.fetchers.viirs.selection import is_better_peak_candidate as is_better_peak_candidate
from atlantis.models.event import FloodEvent
from atlantis.utils.io import download_file, ensure_dir

if TYPE_CHECKING:
    import xarray as xr

DEFAULT_NOAA_VIIRS_BASE_URL = "https://noaa-jpss.s3.amazonaws.com"
DEFAULT_GMU_VIIRS_BASE_URL = "https://jpssflood.gmu.edu/downloads/pub"
VIIRS_SUPPORTED_FORMATS = {"tif", "netcdf", "shapezip", "png"}
VIIRS_IMPLEMENTED_FORMATS = {"tif"}


@dataclass
class SearchDiagnostics:
    """Structured explanation of why a search returned a given result set.

    Populated by :meth:`VIIRSFetcher.search` and exposed via
    :attr:`VIIRSFetcher.last_diagnostics` so the CLI (or any other caller) can
    surface actionable guidance when a fetch yields zero results.
    """

    backend: str
    aoi_count: int = 0
    requested_years: set[int] = field(default_factory=set)
    available_years: set[int] | None = None
    skipped_years: set[int] = field(default_factory=set)
    dates_probed: int = 0
    dates_with_listings: int = 0
    dates_with_matches: int = 0
    result_count: int = 0
    network_failures: int = 0
    last_network_error: str | None = None

    @property
    def missing_aoi_coverage(self) -> bool:
        """True when the event bbox does not intersect any packaged AOI."""
        return self.aoi_count == 0

    @property
    def year_coverage_gap(self) -> bool:
        """True when every requested year is known to be unpublished."""
        return bool(self.skipped_years) and self.skipped_years == self.requested_years

    @property
    def listings_all_empty(self) -> bool:
        """True when listings were attempted but none returned entries."""
        return self.dates_probed > 0 and self.dates_with_listings == 0

    @property
    def no_aoi_match_in_listings(self) -> bool:
        """True when listings returned entries but no AOI filename matched."""
        return self.dates_with_listings > 0 and self.dates_with_matches == 0

    @property
    def network_unreachable(self) -> bool:
        """True when every probed date failed due to a network/HTTP error."""
        return self.dates_probed > 0 and self.network_failures == self.dates_probed


def _date_range(start: datetime, end: datetime) -> list[datetime]:
    """Generate a list of daily datetimes from start to end (inclusive)."""
    dates: list[datetime] = []
    current = start
    while current <= end:
        dates.append(current)
        current += __import__("datetime").timedelta(days=1)
    return dates


def _normalise_backend(value: str) -> str:
    """Normalise and validate backend name."""
    backend = value.strip().lower()
    supported = set(list_backends())
    if backend not in supported:
        supported_str = ", ".join(sorted(supported))
        raise ValueError(f"Unsupported VIIRS backend '{value}'. Expected one of: {supported_str}")
    return backend


def _normalise_format(value: str) -> str:
    """Normalise and validate format name."""
    aliases = {
        "nc": "netcdf",
        "netcdf": "netcdf",
        "shapefile": "shapezip",
        "shape": "shapezip",
        "shapezip": "shapezip",
        "tif": "tif",
        "tiff": "tif",
        "png": "png",
    }
    normalised = aliases.get(value.strip().lower())
    if normalised is None or normalised not in VIIRS_SUPPORTED_FORMATS:
        supported = ", ".join(sorted(VIIRS_SUPPORTED_FORMATS))
        raise ValueError(f"Unsupported VIIRS format '{value}'. Expected one of: {supported}")
    if normalised not in VIIRS_IMPLEMENTED_FORMATS:
        raise NotImplementedError(
            f"VIIRS format '{normalised}' is not implemented yet. Only 'tif' is currently supported."
        )
    return normalised


@register_fetcher("viirs")
class VIIRSFetcher(AbstractFloodFetcher):
    """Fetcher for VIIRS flood detection data.

    VIIRS flood products are derived from the Day-Night Band (DNB)
    and provide inundation detection at 375m resolution.
    """

    source_id: str = "viirs"

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int | None = None,
        backend: str | None = None,
        data_format: str | None = None,
        classify: bool = False,
        stream: bool = False,
        strategy: str = "peak",
        keep_processed: bool = True,
        peak_days_before: int = 0,
        peak_days_after: int = 0,
        max_observations: int = 0,
        peak_priority: str = "post",
    ) -> None:
        """Initialize the VIIRS fetcher.

        Args:
            base_url: Optional base URL for VIIRS data.
            timeout: Request timeout in seconds.
            backend: VIIRS backend selection.
            data_format: VIIRS data format selection.
            classify: Whether to classify pixels into discrete layers.
            stream: If True, stream remote tiles via GDAL ``/vsicurl/``
                instead of downloading them to disk. Saves local storage
                at the cost of network dependency during processing.
            strategy: How to handle multiple dates: "peak" (best flood date),
                "aggregate" (mean/mode), or "all" (every date).
            keep_processed: When True, write intermediate processed/ GeoTIFFs.
            peak_days_before: Days before the computed peak to include when
                filtering dates.  0 means no window filtering.  Requires
                *peak_days_after* > 0 to have any effect (or vice-versa).
            peak_days_after: Days after the computed peak to include.
            max_observations: Maximum number of dates to return after windowing.
                0 means no limit.  Selection order is controlled by
                *peak_priority*.
            peak_priority: How to fill *max_observations* around the peak:
                ``"post"`` (post-event first, then pre), ``"pre"`` (pre-event
                first, then post), or ``"balanced"`` (alternating ±1, ±2, …).
        """
        config = get_config()

        self.backend_name = _normalise_backend(backend or config.fetcher.viirs_backend)
        self.backend = get_backend(self.backend_name)
        self.data_format = _normalise_format(data_format or config.fetcher.viirs_format)
        self.base_url = self._resolve_base_url(base_url, config)
        self.timeout = timeout or config.fetcher.timeout
        self.classify = classify
        self.stream = stream
        self.strategy = strategy
        self.keep_processed = keep_processed
        self.last_diagnostics: SearchDiagnostics | None = None

        if self.strategy not in {"peak", "aggregate", "all"}:
            raise ValueError(f"Invalid strategy '{self.strategy}'. Expected 'peak', 'aggregate', or 'all'.")

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

    def _resolve_base_url(self, override: str | None, config) -> str:
        """Determine the base URL based on backend and configuration."""
        if override is not None:
            return override.rstrip("/")

        if self.backend_name == "noaa_s3":
            url = config.fetcher.viirs_base_url or DEFAULT_NOAA_VIIRS_BASE_URL
        else:
            url = config.fetcher.viirs_legacy_base_url or DEFAULT_GMU_VIIRS_BASE_URL

        return url.rstrip("/")

    @property
    def aoi_path(self) -> Path:
        """Return the packaged AOI grid path.

        The GeoJSON lives alongside the fetcher code (not in ``assets/``)
        so it ships inside the wheel and is available after ``pip install``
        without requiring git or LFS.  In development,
        ``uv run atlantis setup`` ensures it is present.
        """
        return Path(__file__).with_name("data") / "viirs_aois.geojson"

    def _load_aois(self) -> gpd.GeoDataFrame:
        """Load the packaged VIIRS AOI grid."""
        if not self.aoi_path.exists():
            raise FileNotFoundError(
                f"VIIRS AOI grid not found at {self.aoi_path}\n"
                "Run the bootstrap setup to restore it:\n"
                "  uv run atlantis setup\n"
                "  or: uv run python scripts/setup.py"
            )
        return gpd.read_file(self.aoi_path).to_crs("EPSG:4326")

    def _event_dates(self, event: FloodEvent) -> list[datetime]:
        """Expand the event date range into daily datetimes."""
        start = datetime.combine(event.start_date, time.min, tzinfo=timezone.utc)
        end = datetime.combine(event.end_date, time.min, tzinfo=timezone.utc)
        return _date_range(start, end)

    def _intersecting_aois(self, event: FloodEvent) -> gpd.GeoDataFrame:
        """Find packaged AOIs intersecting the event bbox."""
        area_geom = box(*event.bbox)
        aois = self._load_aois()
        hits = aois[aois.intersects(area_geom)].copy()
        return hits.sort_values("AOI_ID").reset_index(drop=True)

    def _materialize_tile(self, source_path: Path, output_dir: Path) -> Path:
        """Return a TIFF tile path for downstream raster processing."""
        if source_path.suffix.lower() == ".zip":
            return self._extract_tif(source_path, output_dir)
        return source_path

    def _extract_tif(self, zip_path: Path, output_dir: Path) -> Path:
        """Extract the first TIFF payload from a VIIRS ZIP file."""
        with ZipFile(zip_path) as archive:
            tif_members = [member for member in archive.namelist() if member.lower().endswith(".tif")]
            if not tif_members:
                raise FileNotFoundError(f"No TIFF file found inside {zip_path}")
            member = tif_members[0]
            target = output_dir / Path(member).name
            if target.exists():
                return target
            archive.extract(member, path=output_dir)
            extracted = output_dir / member
            if extracted != target:
                extracted.replace(target)
                extracted.parent.rmdir()
            return target

    def _apply_peak_window(
        self,
        all_processed: list[tuple[str, "ProcessTilesResult"]],
    ) -> list[tuple[str, "ProcessTilesResult"]]:
        """Apply peak-window filter and max-observations subsampling.

        Returns a filtered (and possibly reordered) copy of *all_processed*.
        If no window/subsample params are set this is a no-op.
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
            # Identify which token the peak window used as the peak (max flood count)
            if surviving:
                peak_token = max(surviving, key=lambda t: flood_pixel_count(processed_map[t]))
            else:
                peak_token = None
            logger.debug(
                "Peak-window [{}, +{}]: {} → {} date(s) (peak={})",
                -self.peak_days_before,
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
                "Subsampled to {} observation(s) (priority='{}', peak={})",
                len(surviving),
                self.peak_priority,
                peak_token,
            )

        surviving_set = set(surviving)
        return [(dt, pr) for dt, pr in all_processed if dt in surviving_set]

    def search(self, event: FloodEvent) -> list[SearchResult]:
        """Search for VIIRS data for the given flood event.

        Populates :attr:`last_diagnostics` describing AOI coverage,
        backend year coverage, and listing/match counts so callers can
        explain why an empty result set was returned.

        Args:
            event: The flood event to search for.

        Returns:
            List of search results.
        """
        event_dates = self._event_dates(event)
        requested_years = {dt.year for dt in event_dates}
        diagnostics = SearchDiagnostics(
            backend=self.backend_name,
            requested_years=requested_years,
        )
        self.last_diagnostics = diagnostics

        aois = self._intersecting_aois(event)
        diagnostics.aoi_count = int(len(aois))
        logger.debug("Loaded AOI grid; {} AOI(s) intersect event bbox {}", len(aois), event.bbox)
        if aois.empty:
            logger.warning(
                "VIIRS search: event bbox {} does not intersect any packaged AOI; nothing to fetch.",
                event.bbox,
            )
            return []

        available_years = self.backend.available_years(self.base_url, self.data_format, self.timeout)
        diagnostics.available_years = available_years
        if available_years is not None:
            logger.debug("Backend '{}' published years: {}", self.backend_name, sorted(available_years))
            diagnostics.skipped_years = requested_years - available_years
            if diagnostics.skipped_years:
                logger.warning(
                    "VIIRS backend '{}' does not publish data for year(s) {} (published years: {}).",
                    self.backend_name,
                    sorted(diagnostics.skipped_years),
                    sorted(available_years),
                )

        results: list[SearchResult] = []
        for event_date in event_dates:
            if available_years is not None and event_date.year not in available_years:
                # Skip dates whose year is known to be unpublished by this backend.
                continue

            diagnostics.dates_probed += 1
            location = self.backend.get_listing_location(self.base_url, event_date, self.data_format)
            try:
                entries = self.backend.get_directory_links(self.base_url, location.locator, self.timeout)
            except requests.RequestException as exc:
                diagnostics.network_failures += 1
                diagnostics.last_network_error = str(exc)
                logger.warning(
                    "VIIRS backend '{}' network error while listing {}: {}",
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
            for row in aois.itertuples():
                filename = self.backend.find_remote_filename(int(row.AOI_ID), entries)
                if filename is None:
                    continue
                date_match_count += 1
                bounds = row.geometry.bounds
                results.append(
                    SearchResult(
                        source_id=self.source_id,
                        item_id=f"viirs:{location.date_token}:{int(row.AOI_ID):03d}",
                        timestamp=event_date,
                        bbox=(bounds[0], bounds[1], bounds[2], bounds[3]),
                        url=self.backend.build_result_url(self.base_url, location.locator, filename),
                        properties={
                            "aoi_id": int(row.AOI_ID),
                            "date": location.date_token,
                            "filename": filename,
                            "backend": self.backend_name,
                            "format": self.data_format,
                        },
                    )
                )
            if date_match_count > 0:
                diagnostics.dates_with_matches += 1
            logger.debug("Date {}: {} entries, {} AOI matches", location.date_token, len(entries), date_match_count)

        diagnostics.result_count = len(results)
        logger.debug("Search complete: {} result(s) across {} date(s)", len(results), diagnostics.dates_probed)
        return results

    def fetch(self, event: FloodEvent, output_dir: Path) -> list[FetchResult]:
        """Fetch VIIRS data for the given flood event.

        Args:
            event: The flood event to fetch data for.
            output_dir: Directory to save downloaded files.

        Returns:
            List of fetch results.
        """
        search_results = self.search(event)
        if not search_results:
            return []

        output_dir = ensure_dir(output_dir)
        raw_dir = ensure_dir(output_dir / "raw") if not self.stream else output_dir / "raw"
        # processed_dir is created later only for surviving dates (after peak-window filter)
        _placeholder_processed_dir = output_dir / "processed"

        # Group results by date
        grouped_results: dict[str, list[SearchResult]] = defaultdict(list)
        for result in search_results:
            grouped_results[result.properties["date"]].append(result)

        area_geom = box(*event.bbox)
        processor = ViirsRasterProcessor(area_geom, classify=self.classify)

        all_processed: list[tuple[str, ProcessTilesResult]] = []

        for date_token, dated_results in sorted(grouped_results.items()):
            mode_label = "stream" if self.stream else "download"
            logger.debug("Processing date {}: {} tile(s) ({} mode)", date_token, len(dated_results), mode_label)
            if self.stream:
                # ── Streaming mode: pass remote URLs directly to processor ──
                tile_sources: list[Path | str] = [
                    result.url for result in sorted(dated_results, key=lambda item: item.properties["aoi_id"])
                ]
                if not tile_sources:
                    continue
                process_result = processor.process_tiles(
                    tile_sources,
                    event.event_id,
                    date_token,
                    _placeholder_processed_dir,
                    write_outputs=False,
                )
            else:
                # ── Download mode (default): download tiles, then process ──
                tile_paths_local = []
                for result in sorted(dated_results, key=lambda item: item.properties["aoi_id"]):
                    filename = result.properties["filename"]
                    download_path = raw_dir / filename
                    download_file(result.url, output_path=download_path)
                    tile_paths_local.append(self._materialize_tile(download_path, raw_dir))

                if not tile_paths_local:
                    continue
                process_result = processor.process_tiles(
                    tile_paths_local,
                    event.event_id,
                    date_token,
                    _placeholder_processed_dir,
                    write_outputs=False,
                )

            if process_result is not None:
                all_processed.append((date_token, process_result))

        if not all_processed:
            return []

        # ── Peak-window filter + subsampling ─────────────────────────
        all_processed = self._apply_peak_window(all_processed)

        if not all_processed:
            return []

        # ── Write surviving dates to processed/ (if requested) ───────
        if self.keep_processed:
            processed_dir = ensure_dir(output_dir / "processed")
            for date_token, proc_result in all_processed:
                base_name = f"{event.event_id}_{date_token}_viirs"
                if self.classify:
                    paths = OutputPaths(
                        flood_fraction=processed_dir / f"{base_name}_flood_fraction.tif",
                        quality_mask=processed_dir / f"{base_name}_quality_mask.tif",
                        permanent_water=processed_dir / f"{base_name}_permanent_water.tif",
                        extra={
                            name: processed_dir / f"{base_name}_{name}.tif"
                            for name in proc_result.processed.extra_layers
                        },
                    )
                else:
                    paths = OutputPaths(raw=processed_dir / f"{base_name}_raw.tif")
                updated_result = ProcessTilesResult(
                    paths=paths,
                    metadata=proc_result.metadata,
                    processed=proc_result.processed,
                )
                processor.write_processed(updated_result)
                # Replace the entry so downstream code sees the correct paths
                all_processed[all_processed.index((date_token, proc_result))] = (date_token, updated_result)

        # ── Strategy dispatch ──────────────────────────────────────────
        logger.debug("Strategy '{}': {} date(s) after window filter", self.strategy, len(all_processed))
        if self.strategy == "all":
            if self.keep_processed:
                return [self._fetch_result_from_process(event.event_id, dt, pr) for dt, pr in all_processed]
            else:
                # Per-date in-memory datasets (no processed/ writes)
                return [self._in_memory_fetch_result(event.event_id, dt, pr) for dt, pr in all_processed]

        elif self.strategy == "aggregate":
            all_tiles = [pr.processed for _, pr in all_processed]
            aggregated = ViirsRasterProcessor.aggregate_tiles(all_tiles)
            # If keep_processed, we could write the aggregate, but for now just return in-memory
            return [
                self._in_memory_fetch_result(
                    event.event_id,
                    "aggregated",
                    ProcessTilesResult(OutputPaths(), all_processed[0][1].metadata, aggregated),
                )
            ]

        else:  # peak
            best_date_token, best_result = max(all_processed, key=lambda item: flood_pixel_count(item[1].processed))
            logger.debug(
                "Peak date selected: {} ({} flood pixels)",
                best_date_token,
                flood_pixel_count(best_result.processed),
            )
            if self.keep_processed:
                return [self._fetch_result_from_process(event.event_id, best_date_token, best_result)]
            else:
                return [self._in_memory_fetch_result(event.event_id, best_date_token, best_result)]

    @staticmethod
    def _fetch_result_from_process(event_id: str, date_token: str, process_result: ProcessTilesResult) -> FetchResult:
        paths = process_result.paths
        return FetchResult(
            event_id=event_id,
            source_id="viirs",
            files=[
                p for p in (paths.raw, paths.flood_fraction, paths.quality_mask, paths.permanent_water) if p is not None
            ],
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

    def to_dataset(self, result: FetchResult) -> "xr.Dataset":
        """Convert VIIRS fetch result to xarray Dataset.

        Args:
            result: The fetch result to convert.

        Returns:
            xarray Dataset with VIIRS data.
        """
        try:
            import rioxarray as rxr
            import xarray as xr
        except ImportError as exc:
            raise ImportError("rioxarray and xarray are required to read VIIRS datasets") from exc

        if result.dataset is not None:
            return result.dataset

        files_by_name = {path.name: path for path in result.files}

        raw_path = next((path for name, path in files_by_name.items() if name.endswith("_raw.tif")), None)

        variables = {}
        if raw_path:
            variables["raw"] = rxr.open_rasterio(raw_path).squeeze(drop=True).rename("raw")
        else:
            obs_path = next(path for name, path in files_by_name.items() if name.endswith("_flood_fraction.tif"))
            quality_path = next(path for name, path in files_by_name.items() if name.endswith("_quality_mask.tif"))
            permanent_water_path = next(
                path for name, path in files_by_name.items() if name.endswith("_permanent_water.tif")
            )

            flood_fraction = rxr.open_rasterio(obs_path).squeeze(drop=True).astype("float32")
            nodata = flood_fraction.rio.nodata
            if nodata is None:
                nodata = CLASSIFIED_FLOOD_NODATA
            flood_fraction = flood_fraction.where(flood_fraction != nodata) / 100.0
            variables["flood_fraction"] = flood_fraction.rename("flood_fraction")
            variables["quality_mask"] = (
                rxr.open_rasterio(quality_path).squeeze(drop=True).astype("uint8").rename("quality_mask")
            )
            variables["permanent_water"] = (
                rxr.open_rasterio(permanent_water_path).squeeze(drop=True).astype("uint8").rename("permanent_water")
            )

            # Extra derived layers (e.g. cloud_mask, shadow): match by the
            # registry's derived layer names so new layers load without edits.
            from atlantis.fetchers.viirs.layers import registry

            core = {"flood_fraction", "quality_mask", "permanent_water"}
            for spec in registry.list_derived():
                if spec.name in core:
                    continue
                extra_path = next(
                    (path for name, path in files_by_name.items() if name.endswith(f"_{spec.name}.tif")),
                    None,
                )
                if extra_path is not None:
                    variables[spec.name] = (
                        rxr.open_rasterio(extra_path).squeeze(drop=True).astype(spec.dtype).rename(spec.name)
                    )

        dataset = xr.Dataset(variables)
        dataset.attrs["source_id"] = self.source_id
        dataset.attrs["event_id"] = result.event_id
        return dataset
