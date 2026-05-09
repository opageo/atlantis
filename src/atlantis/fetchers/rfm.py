"""Regional Flood Model (RFM) fetcher stub for future implementation.

RFM provides modelled flood extent from hydrological/hydrodynamic models.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult, SearchResult
from atlantis.fetchers.registry import register_fetcher
from atlantis.models.event import FloodEvent

if TYPE_CHECKING:
    import xarray as xr


@register_fetcher("rfm")
class RFMFetcher(AbstractFloodFetcher):
    """Fetcher stub for Regional Flood Model data.

    This is a placeholder for Phase C implementation.
    RFM will provide modelled flood extent from operational
    hydrological models (e.g., EFAS, GloFAS).
    """

    source_id: str = "rfm"

    def __init__(self, api_url: str | None = None) -> None:
        """Initialize the RFM fetcher.

        Args:
            api_url: Optional API URL for RFM data.
        """
        self.api_url = api_url

    def search(self, event: FloodEvent) -> list[SearchResult]:
        """Search for RFM data (stub - not implemented).

        Args:
            event: The flood event to search for.

        Returns:
            Empty list - not yet implemented.
        """
        # TODO: Implement RFM search for Phase C
        return []

    def fetch(self, event: FloodEvent, output_dir: Path) -> list[FetchResult]:
        """Fetch RFM data (stub - not implemented).

        Args:
            event: The flood event to fetch data for.
            output_dir: Directory to save downloaded files.

        Returns:
            Empty list - not yet implemented.
        """
        # TODO: Implement RFM download for Phase C
        return []

    def to_dataset(self, result: FetchResult) -> "xr.Dataset":  # type: ignore[name-defined]
        """Convert RFM result (not implemented).

        Args:
            result: The fetch result to convert.

        Returns:
            Not implemented.
        """
        raise NotImplementedError("RFM to_dataset not yet implemented (Phase C)")
