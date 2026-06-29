"""Global Flood Monitor (GFM) fetcher using STAC/EODC API.

GFM provides near-real-time flood extent data from Sentinel-1 SAR sensors.
Data is accessed via the EODC STAC API and processed through:
load (native CRS) → coarsen (max-pool) → reproject (EPSG:4326) → accumulate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from rasterio.enums import Resampling

from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult, SearchResult
from atlantis.fetchers.gfm.backend import DEFAULT_GFM_STAC_URL, GfmStacBackend
from atlantis.fetchers.gfm.dataset import processed_tile_to_dataset
from atlantis.fetchers.gfm.processor import (
    DEFAULT_COARSEN_FACTOR,
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

__all__ = ["GFMFetcher"]


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
        classify: When True (default), derive flood_fraction / quality_mask /
            permanent_water.  When False, emit the native band codes
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
    ) -> None:
        """Initialize the GFM fetcher.

        Args:
            api_url: Optional STAC API URL. Defaults to EODC endpoint.
            coarsen_factor: Coarsen factor for SAR data (default 4).
                Ignored when *classify* is False.
            resampling: Resampling method for reprojection.
                Ignored when *classify* is False (nearest-neighbour used instead).
            classify: When True (default), derive flood_fraction / quality_mask /
                permanent_water from per-pixel accumulator counts. When False,
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
        """
        self.api_url = api_url or DEFAULT_GFM_STAC_URL
        self.coarsen_factor = coarsen_factor
        self.resampling = resampling
        self.classify = classify
        self.strategy = strategy
        self.keep_processed = keep_processed
        self._backend = GfmStacBackend(api_url=self.api_url)

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

        Args:
            event: The flood event to search for.

        Returns:
            List of search results (one per STAC item).
        """
        items = self._backend.search(event)
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
        items = self._backend.search(event)
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
        )

        date_results: list[tuple[str, GfmProcessedTile]] = []
        for date_token, date_items in sorted(date_groups.items()):
            result = processor.process_items(
                date_items,
                event_id=event.event_id,
                date_token=date_token,
                output_dir=output_dir if self.keep_processed else None,
                write_outputs=self.keep_processed,
            )
            if result is not None:
                date_results.append((date_token, result.processed))

        if not date_results:
            logger.warning("No valid data after processing for event {}", event.event_id)
            return []

        date_results = self._apply_peak_window(date_results)
        if not date_results:
            return []

        # Apply strategy
        return self._apply_strategy(date_results, event, output_dir)

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
    ) -> list[FetchResult]:
        """Apply date selection strategy to processed results."""
        if self.strategy == "peak":
            return self._strategy_peak(date_results, event, output_dir)
        elif self.strategy == "aggregate":
            return self._strategy_aggregate(date_results, event, output_dir)
        elif self.strategy == "all":
            return self._strategy_all(date_results, event, output_dir)
        else:
            logger.warning("Unknown strategy '{}', falling back to aggregate", self.strategy)
            return self._strategy_aggregate(date_results, event, output_dir)

    def _strategy_peak(
        self,
        date_results: list[tuple[str, GfmProcessedTile]],
        event: FloodEvent,
        output_dir: Path,
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
        return [self._build_fetch_result(best_tile, event, best_date)]

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
        return [self._build_fetch_result(aggregated, event, date_token)]

    def _strategy_all(
        self,
        date_results: list[tuple[str, GfmProcessedTile]],
        event: FloodEvent,
        output_dir: Path,
    ) -> list[FetchResult]:
        """Return each date as a separate FetchResult."""
        results = []
        for date_token, tile in date_results:
            results.append(self._build_fetch_result(tile, event, date_token))
        return results

    def _build_fetch_result(
        self,
        tile: GfmProcessedTile,
        event: FloodEvent,
        date_token: str,
    ) -> FetchResult:
        """Build a FetchResult with an in-memory dataset."""
        from atlantis.models.metadata import TileMetadata

        ds = processed_tile_to_dataset(tile, event_id=event.event_id, source_id=self.source_id)
        metadata = TileMetadata(
            event_id=event.event_id,
            source_id=self.source_id,
            fetch_timestamp=datetime.now(timezone.utc),
            crs="EPSG:4326",
            resolution=0.0,
            bbox=event.bbox,
            cloud_fraction=tile.cloud_fraction,
            permanent_water_mask_available=self.classify,
        )

        return FetchResult(
            event_id=event.event_id,
            source_id=self.source_id,
            files=[],
            metadata=metadata,
            date_token=date_token,
            dataset=ds,
        )

    def to_dataset(self, result: FetchResult) -> "xr.Dataset":
        """Convert GFM fetch result to xarray Dataset.

        Args:
            result: The fetch result to convert.

        Returns:
            xarray Dataset with flood_fraction / quality_mask / permanent_water
            (classified mode) or ensemble_flood_extent / reference_water_mask
            (native mode).
        """
        if result.dataset is not None:
            return result.dataset

        # Fallback: read from files if dataset not in memory
        if result.files:
            import rioxarray as rxr

            # Read first file; name by its stem suffix to preserve band identity
            first = result.files[0]
            # Infer variable name from filename suffix (e.g. *_flood_fraction.tif)
            stem = first.stem
            var_name = stem.split("_gfm_")[-1] if "_gfm_" in stem else "band"
            ds = rxr.open_rasterio(first).squeeze(drop=True).to_dataset(name=var_name)
            return ds

        raise ValueError("FetchResult has neither in-memory dataset nor files")
