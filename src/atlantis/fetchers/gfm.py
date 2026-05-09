"""Global Flood Monitor (GFM) fetcher using STAC/EODC API.

GFM provides near-real-time flood extent data from multiple SAR sensors.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult, SearchResult
from atlantis.fetchers.registry import register_fetcher
from atlantis.models.event import FloodEvent

if TYPE_CHECKING:
    import xarray as xr


@register_fetcher("gfm")
class GFMFetcher(AbstractFloodFetcher):
    """Fetcher for Global Flood Monitor data via STAC/EODC.

    GFM provides daily flood inundation maps derived from Sentinel-1,
    Sentinel-2, and Landsat sensors.
    """

    source_id: str = "gfm"

    def __init__(self, api_url: str | None = None) -> None:
        """Initialize the GFM fetcher.

        Args:
            api_url: Optional STAC API URL. Defaults to EODC endpoint.
        """
        self.api_url = api_url or "https://stac.eodc.eu/api/v1"

    def search(self, event: FloodEvent) -> list[SearchResult]:
        """Search for GFM data for the given flood event.

        Args:
            event: The flood event to search for.

        Returns:
            List of search results.
        """
        # TODO: Implement STAC API search
        # Expected implementation:
        # 1. Connect to STAC endpoint
        # 2. Query for GFM flood products within bbox and date range
        # 3. Return SearchResult objects
        return []

    def fetch(self, event: FloodEvent, output_dir: Path) -> list[FetchResult]:
        """Fetch GFM data for the given flood event.

        Args:
            event: The flood event to fetch data for.
            output_dir: Directory to save downloaded files.

        Returns:
            List of fetch results.
        """
        # TODO: Implement data download
        # Expected implementation:
        # 1. Search for available data
        # 2. Download TIFF files to output_dir
        # 3. Return FetchResult with file paths and metadata
        return []

    def to_dataset(self, result: FetchResult) -> "xr.Dataset":  # type: ignore[name-defined]
        """Convert GFM fetch result to xarray Dataset.

        Args:
            result: The fetch result to convert.

        Returns:
            xarray Dataset with GFM data.
        """
        # TODO: Implement conversion
        # GFM standard variables:
        # - flood_sentinel1: SAR-derived flood extent
        # - flood_sentinel2: optical flood extent
        # - quality: quality assessment
        raise NotImplementedError("GFM to_dataset not yet implemented")
