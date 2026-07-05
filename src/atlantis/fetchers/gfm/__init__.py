"""Global Flood Monitor (GFM) fetcher using STAC/EODC API.

GFM provides near-real-time flood extent data from Sentinel-1 SAR sensors.
Data is accessed via the EODC STAC API and processed through:
load (native CRS) → coarsen (max-pool) → reproject (EPSG:4326) → accumulate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from rasterio.enums import Resampling

from atlantis.config import FetcherConfig
from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult, SearchResult
from atlantis.fetchers.gfm.backend import DEFAULT_GFM_STAC_URL, GfmStacBackend
from atlantis.fetchers.gfm.dataset import processed_tile_to_dataset
from atlantis.fetchers.gfm.processor import (
    DEFAULT_COARSEN_FACTOR,
    GFM_NODATA,
    GfmOutputPaths,
    GfmProcessedTile,
    GfmRasterProcessor,
)
from atlantis.fetchers.gfm.selection import (
    flood_pixel_count,
    is_better_peak_candidate,
    select_peak_window,
    subsample_around_peak,
)
from atlantis.fetchers.registry import register_fetcher
from atlantis.models.event import FloodEvent

if TYPE_CHECKING:
    import xarray as xr

__all__ = ["GFMFetcher", "GfmSearchDiagnostics"]


@dataclass
class GfmSearchDiagnostics:
    """Structured explanation of why a GFM search returned a given result set.

    Populated by :meth:`GFMFetcher.search` and exposed via
    :attr:`GFMFetcher.last_diagnostics` so the CLI (or any other caller)
    can surface actionable guidance when a fetch yields zero results.
    """

    api_url: str
    items_found: int = 0
    dates_found: int = 0
    network_failure: bool = False
    last_network_error: str | None = None

    @property
    def network_unreachable(self) -> bool:
        """True when the STAC search failed due to a network/connection error."""
        return self.network_failure

    @property
    def no_items_found(self) -> bool:
        """True when the STAC search succeeded but returned no items."""
        return not self.network_failure and self.items_found == 0


@register_fetcher("gfm")
class GFMFetcher(AbstractFloodFetcher):
    """Fetcher for Global Flood Monitor data via STAC/EODC.

    GFM provides daily flood inundation maps derived from Sentinel-1 SAR.
    Data is loaded on-the-fly from Cloud-Optimised GeoTIFFs via the EODC STAC
    API — no separate download step is needed.

    Attributes:
        source_id: "gfm"
        api_url: STAC API endpoint.
        coarsen_factor: Spatial coarsening before reprojection.
        resampling: Resampling method for reprojection.
        classify: When True (default), derive water_fraction / flood_fraction /
            reference_water plus native-code extras. When False, emit the native band codes
            ensemble_flood_extent and reference_water_mask.
        strategy: Date selection strategy ("peak", "aggregate", "all"). Default: "peak".
        keep_processed: Whether to write intermediate GeoTIFFs.
    """

    source_id: str = "gfm"

    def __init__(
        self,
        api_url: str | None = None,
        coarsen_factor: int = DEFAULT_COARSEN_FACTOR,
        resampling: Resampling = Resampling.average,
        classify: bool = True,
        strategy: str = "peak",
        keep_processed: bool = True,
        peak_days_before: int = 0,
        peak_days_after: int = 0,
        max_observations: int = 0,
        peak_priority: str = "post",
        max_retries: int | None = None,
    ) -> None:
        """Initialize the GFM fetcher.

        Args:
            api_url: Optional STAC API URL. Defaults to EODC endpoint.
            coarsen_factor: Coarsen factor for SAR data (default 4).
                Ignored when *classify* is False.
            resampling: Resampling method for reprojection.
                Ignored when *classify* is False (nearest-neighbour used instead).
            classify: When True (default), derive water_fraction / flood_fraction /
                reference_water from per-pixel accumulators and carry native-code extras. When False,
                emit the native ``ensemble_flood_extent`` and
                ``reference_water_mask`` band codes as-is, reprojected to the
                canonical 1-arcmin grid with nearest-neighbour.
            strategy: Date selection strategy: "peak", "aggregate", or "all".
            keep_processed: Whether to write intermediate processed files.
            peak_days_before: Days before the computed peak to include when
                filtering dates. 0 means no window filtering.
            peak_days_after: Days after the computed peak to include.
            max_observations: Maximum number of dates to return after windowing.
                0 means no limit. Selection order is controlled by
                *peak_priority*.
            peak_priority: How to fill *max_observations* around the peak:
                ``"post"`` (post-event first), ``"pre"`` (pre-event first),
                or ``"balanced"`` (alternating ±1, ±2, …).
            max_retries: Number of retries for transient GFM tile-read failures.
                Defaults to ``FetcherConfig.max_retries`` (3).
        """
        self.api_url = api_url or DEFAULT_GFM_STAC_URL
        self.coarsen_factor = coarsen_factor
        self.resampling = resampling
        self.classify = classify
        self.strategy = strategy
        self.keep_processed = keep_processed
        self.max_retries = max_retries if max_retries is not None else FetcherConfig().max_retries
        self._backend = GfmStacBackend(api_url=self.api_url)
        self.last_diagnostics: GfmSearchDiagnostics | None = None

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

    def search(self, event: FloodEvent) -> list[SearchResult]:
        """Search for GFM data for the given flood event.

        Populates :attr:`last_diagnostics` describing STAC item counts,
        date coverage, and any network errors so callers can explain why
        an empty result set was returned.

        Args:
            event: The flood event to search for.

        Returns:
            List of search results (one per STAC item).
        """
        diagnostics = GfmSearchDiagnostics(api_url=self.api_url)
        self.last_diagnostics = diagnostics

        try:
            items = self._backend.search(event)
        except Exception as exc:  # noqa: BLE001
            diagnostics.network_failure = True
            diagnostics.last_network_error = str(exc)
            logger.warning("GFM STAC search failed: {}", exc)
            return []

        diagnostics.items_found = len(items)
        date_groups = self._backend.group_items_by_date(items)
        diagnostics.dates_found = len(date_groups)

        results: list[SearchResult] = []

        for item in items:
            dt = item.datetime or datetime.now(timezone.utc)
            bbox = item.bbox or event.bbox

            results.append(
                SearchResult(
                    source_id=self.source_id,
                    item_id=item.id,
                    timestamp=dt,
                    bbox=(bbox[0], bbox[1], bbox[2], bbox[3]),
                    cloud_fraction=0.0,
                    url=item.self_href or "",
                    properties=dict(item.properties),
                )
            )

        return results

    def fetch(self, event: FloodEvent, output_dir: Path) -> list[FetchResult]:
        """Fetch and process GFM data for the given flood event.

        Pipeline:
        1. Search STAC for items in bbox × date range.
        2. Group items by date.
        3. Process each date group (coarsen + reproject + classify).
        4. Apply strategy (peak/aggregate/all).
        5. Return FetchResult(s) with in-memory dataset and/or files.

        Args:
            event: The flood event to fetch data for.
            output_dir: Directory to save processed files.

        Returns:
            List of fetch results.
        """
        items = self._backend.search(event)  # diagnostics already set by search() when called first
        if not items:
            logger.warning("No GFM items found for event {}", event.event_id)
            return []

        # Group items by acquisition date
        date_groups = self._backend.group_items_by_date(items)
        if not date_groups:
            logger.warning("Could not group any items by date for event {}", event.event_id)
            return []

        logger.info(
            "GFM fetch: {} items across {} dates for event {}",
            len(items),
            len(date_groups),
            event.event_id,
        )

        # Process each date group
        processor = GfmRasterProcessor(
            bbox=event.bbox,
            coarsen_factor=self.coarsen_factor,
            resampling=self.resampling,
            classify=self.classify,
            max_retries=self.max_retries,
        )

        date_results: list[tuple[str, GfmProcessedTile]] = []
        for date_token, date_items in sorted(date_groups.items()):
            result = processor.process_items(
                date_items,
                event_id=event.event_id,
                date_token=date_token,
                output_dir=None,
                write_outputs=False,
            )
            if result is not None:
                date_results.append((date_token, result.processed))

        if not date_results:
            logger.warning("No valid data after processing for event {}", event.event_id)
            return []

        date_results = self._apply_peak_window(date_results)
        if not date_results:
            return []

        written_paths: dict[str, GfmOutputPaths] = {}

        # Write processed/ only for surviving dates (after peak-window filter),
        # mirroring VIIRS/MODIS — avoids persisting pre-filter dates to disk.
        if self.keep_processed:
            for date_token, tile in date_results:
                written_paths[date_token] = processor.write_processed(tile, event.event_id, date_token, output_dir)

        # Apply strategy
        return self._apply_strategy(date_results, event, output_dir, written_paths if self.keep_processed else None)

    def _apply_peak_window(
        self,
        date_results: list[tuple[str, GfmProcessedTile]],
    ) -> list[tuple[str, GfmProcessedTile]]:
        """Apply peak-window filter and max-observations subsampling.

        No-op when neither *peak_days_before/after* nor *max_observations*
        is set. Mirrors
        :meth:`atlantis.fetchers.viirs.VIIRSFetcher._apply_peak_window`.
        """
        uses_window = self.peak_days_before > 0 or self.peak_days_after > 0
        uses_subsample = self.max_observations > 0

        if not uses_window and not uses_subsample:
            return date_results

        date_tokens = [dt for dt, _ in date_results]
        processed_map = {dt: tile for dt, tile in date_results}

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
                "GFM peak-window [-{}, +{}]: {} → {} date(s) (peak={})",
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
                "GFM subsampled to {} observation(s) (priority='{}', peak={})",
                len(surviving),
                self.peak_priority,
                peak_token,
            )

        surviving_set = set(surviving)
        return [(dt, tile) for dt, tile in date_results if dt in surviving_set]

    def _apply_strategy(
        self,
        date_results: list[tuple[str, GfmProcessedTile]],
        event: FloodEvent,
        output_dir: Path,
        written_paths: dict[str, GfmOutputPaths] | None = None,
    ) -> list[FetchResult]:
        """Apply date selection strategy to processed results."""
        if self.strategy == "peak":
            return self._strategy_peak(date_results, event, output_dir, written_paths)
        elif self.strategy == "aggregate":
            return self._strategy_aggregate(date_results, event, output_dir)
        elif self.strategy == "all":
            return self._strategy_all(date_results, event, output_dir, written_paths)
        else:
            logger.warning("Unknown strategy '{}', falling back to aggregate", self.strategy)
            return self._strategy_aggregate(date_results, event, output_dir)

    def _strategy_peak(
        self,
        date_results: list[tuple[str, GfmProcessedTile]],
        event: FloodEvent,
        output_dir: Path,
        written_paths: dict[str, GfmOutputPaths] | None = None,
    ) -> list[FetchResult]:
        """Pick the date with the most flood pixels."""
        best_date = ""
        best_tile: GfmProcessedTile | None = None
        best_count = -1

        for date_token, tile in date_results:
            count = flood_pixel_count(tile)
            if is_better_peak_candidate(count, best_count):
                best_count = count
                best_date = date_token
                best_tile = tile

        if best_tile is None:
            return []

        logger.info("Peak strategy selected date {} ({} flood pixels)", best_date, best_count)
        if written_paths and best_date in written_paths:
            return [self._file_backed_fetch_result(best_tile, event, best_date, written_paths[best_date])]
        return [self._in_memory_fetch_result(best_tile, event, best_date)]

    def _strategy_aggregate(
        self,
        date_results: list[tuple[str, GfmProcessedTile]],
        event: FloodEvent,
        output_dir: Path,
    ) -> list[FetchResult]:
        """Aggregate all dates into a single result."""
        tiles = [tile for _, tile in date_results]
        aggregated = GfmRasterProcessor.aggregate_tiles(tiles)
        if aggregated is None:
            return []

        # Use full date range as the token
        dates = sorted(dt for dt, _ in date_results)
        date_token = f"{dates[0]}_{dates[-1]}" if len(dates) > 1 else dates[0]

        logger.info("Aggregate strategy: combined {} dates", len(tiles))
        return [self._in_memory_fetch_result(aggregated, event, date_token)]

    def _strategy_all(
        self,
        date_results: list[tuple[str, GfmProcessedTile]],
        event: FloodEvent,
        output_dir: Path,
        written_paths: dict[str, GfmOutputPaths] | None = None,
    ) -> list[FetchResult]:
        """Return each date as a separate FetchResult."""
        results = []
        for date_token, tile in date_results:
            if written_paths and date_token in written_paths:
                results.append(self._file_backed_fetch_result(tile, event, date_token, written_paths[date_token]))
            else:
                results.append(self._in_memory_fetch_result(tile, event, date_token))
        return results

    def _build_metadata(
        self,
        tile: GfmProcessedTile,
        event: FloodEvent,
    ):
        """Build TileMetadata for a processed GFM tile."""
        from atlantis.models.metadata import TileMetadata

        return TileMetadata(
            event_id=event.event_id,
            source_id=self.source_id,
            fetch_timestamp=datetime.now(timezone.utc),
            crs="EPSG:4326",
            resolution=0.0,
            bbox=event.bbox,
            cloud_fraction=tile.cloud_fraction,
            permanent_water_mask_available=self.classify,
        )

    @staticmethod
    def _paths_to_files(paths: GfmOutputPaths) -> list[Path]:
        return [
            path
            for path in (
                paths.water_fraction,
                paths.flood_fraction,
                paths.reference_water,
                paths.ensemble_flood_extent,
                paths.reference_water_mask,
                *paths.extra.values(),
            )
            if path is not None
        ]

    def _file_backed_fetch_result(
        self,
        tile: GfmProcessedTile,
        event: FloodEvent,
        date_token: str,
        paths: GfmOutputPaths,
    ) -> FetchResult:
        """Build a FetchResult backed by written processed GeoTIFFs."""
        return FetchResult(
            event_id=event.event_id,
            source_id=self.source_id,
            files=self._paths_to_files(paths),
            metadata=self._build_metadata(tile, event),
            date_token=date_token,
        )

    def _in_memory_fetch_result(
        self,
        tile: GfmProcessedTile,
        event: FloodEvent,
        date_token: str,
    ) -> FetchResult:
        """Build a FetchResult with an in-memory dataset."""
        ds = processed_tile_to_dataset(tile, event_id=event.event_id, source_id=self.source_id)

        return FetchResult(
            event_id=event.event_id,
            source_id=self.source_id,
            files=[],
            metadata=self._build_metadata(tile, event),
            date_token=date_token,
            dataset=ds,
        )

    def _build_fetch_result(
        self,
        tile: GfmProcessedTile,
        event: FloodEvent,
        date_token: str,
    ) -> FetchResult:
        """Backward-compatible alias for the in-memory fetch-result builder."""
        return self._in_memory_fetch_result(tile, event, date_token)

    def to_dataset(self, result: FetchResult) -> "xr.Dataset":
        """Convert GFM fetch result to xarray Dataset.

        Args:
            result: The fetch result to convert.

        Returns:
            xarray Dataset with water_fraction / flood_fraction /
            reference_water (classified mode, plus native-code extras when
            present) or ensemble_flood_extent / reference_water_mask
            (native mode).
        """
        try:
            import rioxarray as rxr
            import xarray as xr
        except ImportError as exc:
            raise ImportError("rioxarray and xarray are required to read GFM datasets") from exc

        if result.dataset is not None:
            return result.dataset

        if result.files:
            from atlantis.fetchers.gfm.layers import registry

            derived_specs = {spec.name: spec for spec in registry.list_derived()}
            native_specs = {spec.name: spec for spec in registry.list_native()}

            variables: dict = {}
            for path in result.files:
                stem = path.stem
                name = stem.split("_gfm_")[-1] if "_gfm_" in stem else stem
                spec = derived_specs.get(name) or native_specs.get(name)
                if spec is None:
                    continue

                layer = rxr.open_rasterio(path).squeeze(drop=True)
                if spec.dtype == "float32":
                    nodata = layer.rio.nodata
                    if nodata is None:
                        nodata = GFM_NODATA
                    layer = layer.astype("float32").where(layer != nodata) / 100.0
                else:
                    layer = layer.astype(spec.dtype)
                variables[name] = layer.rename(name)

            dataset = xr.Dataset(variables)
            dataset.attrs["source_id"] = self.source_id
            dataset.attrs["event_id"] = result.event_id
            return dataset

        raise ValueError("FetchResult has neither in-memory dataset nor files")
