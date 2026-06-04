"""Global Flood Monitor (GFM) fetcher using STAC/EODC API.

GFM provides near-real-time flood extent data from Sentinel-1 SAR sensors.
Data is accessed via the EODC STAC API and processed through:
load (native CRS) → coarsen (max-pool) → reproject (EPSG:4326) → accumulate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from rasterio.enums import Resampling

from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult, SearchResult
from atlantis.fetchers.gfm.backend import DEFAULT_GFM_STAC_URL, GfmStacBackend
from atlantis.fetchers.gfm.dataset import processed_tile_to_dataset
from atlantis.fetchers.gfm.processor import (
    DEFAULT_COARSEN_FACTOR,
    GfmProcessedTile,
    GfmRasterProcessor,
)
from atlantis.fetchers.registry import register_fetcher
from atlantis.models.event import FloodEvent

if TYPE_CHECKING:
    import xarray as xr

logger = logging.getLogger(__name__)

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
        strategy: Date selection strategy ("peak", "aggregate", "all").
        keep_processed: Whether to write intermediate GeoTIFFs.
    """

    source_id: str = "gfm"

    def __init__(
        self,
        api_url: str | None = None,
        coarsen_factor: int = DEFAULT_COARSEN_FACTOR,
        resampling: Resampling = Resampling.average,
        strategy: str = "aggregate",
        keep_processed: bool = True,
    ) -> None:
        """Initialize the GFM fetcher.

        Args:
            api_url: Optional STAC API URL. Defaults to EODC endpoint.
            coarsen_factor: Coarsen factor for SAR data (default 4).
            resampling: Resampling method for reprojection.
            strategy: Date selection strategy: "peak", "aggregate", or "all".
            keep_processed: Whether to write intermediate processed files.
        """
        self.api_url = api_url or DEFAULT_GFM_STAC_URL
        self.coarsen_factor = coarsen_factor
        self.resampling = resampling
        self.strategy = strategy
        self.keep_processed = keep_processed
        self._backend = GfmStacBackend(api_url=self.api_url)

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
            logger.warning("No GFM items found for event %s", event.event_id)
            return []

        # Group items by acquisition date
        date_groups = self._backend.group_items_by_date(items)
        if not date_groups:
            logger.warning("Could not group any items by date for event %s", event.event_id)
            return []

        logger.info(
            "GFM fetch: %d items across %d dates for event %s",
            len(items),
            len(date_groups),
            event.event_id,
        )

        # Process each date group
        processor = GfmRasterProcessor(
            bbox=event.bbox,
            coarsen_factor=self.coarsen_factor,
            resampling=self.resampling,
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
            logger.warning("No valid data after processing for event %s", event.event_id)
            return []

        # Apply strategy
        return self._apply_strategy(date_results, event, output_dir)

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
            logger.warning("Unknown strategy '%s', falling back to aggregate", self.strategy)
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
            count = GfmRasterProcessor.flood_pixel_count(tile)
            if count > best_count:
                best_count = count
                best_date = date_token
                best_tile = tile

        if best_tile is None:
            return []

        logger.info("Peak strategy selected date %s (%d flood pixels)", best_date, best_count)
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

        logger.info("Aggregate strategy: combined %d dates", len(tiles))
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
            permanent_water_mask_available=True,
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
            xarray Dataset with flood_fraction, quality_mask, permanent_water.
        """
        if result.dataset is not None:
            return result.dataset

        # Fallback: read from files if dataset not in memory
        if result.files:
            import rioxarray as rxr

            # Assume first file is the flood_fraction
            ds = rxr.open_rasterio(result.files[0]).squeeze(drop=True).to_dataset(name="flood_fraction")
            return ds

        raise ValueError("FetchResult has neither in-memory dataset nor files")
