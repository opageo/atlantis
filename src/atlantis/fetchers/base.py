"""Base classes for flood data fetchers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from atlantis.models.event import FloodEvent
from atlantis.models.metadata import TileMetadata

if TYPE_CHECKING:
    import xarray as xr


@runtime_checkable
class FloodFetcher(Protocol):
    """Protocol defining the flood fetcher interface."""

    source_id: str

    def search(self, event: FloodEvent) -> list["SearchResult"]:
        """Search for available data for the given flood event."""
        ...

    def fetch(self, event: FloodEvent, output_dir: Path) -> list["FetchResult"]:
        """Fetch raw data for the given flood event."""
        ...


@dataclass
class SearchResult:
    """Result from searching a data source.

    Attributes:
        source_id: Data source identifier.
        item_id: Unique item identifier from the source.
        timestamp: Acquisition timestamp.
        bbox: Bounding box as (west, south, east, north).
        cloud_fraction: Cloud cover fraction (0.0-1.0).
        url: URL or path to the data.
        properties: Additional properties from the source.
    """

    source_id: str
    item_id: str
    timestamp: datetime
    bbox: tuple[float, float, float, float]
    cloud_fraction: float = 0.0
    url: str = ""
    properties: dict = field(default_factory=dict)


@dataclass
class FetchResult:
    """Result from fetching data from a source.

    Attributes:
        event_id: Flood event identifier.
        source_id: Data source identifier.
        files: List of downloaded file paths.
        metadata: Tile metadata.
        timestamp: Fetch timestamp.
    """

    event_id: str
    source_id: str
    files: list[Path]
    metadata: TileMetadata
    timestamp: datetime = field(default_factory=datetime.utcnow)


class AbstractFloodFetcher(ABC):
    """Abstract base class for flood data fetchers.

    Subclasses must implement search, fetch, and to_dataset methods.
    Use the @register_fetcher decorator to register with the global registry.
    """

    source_id: str = ""

    @abstractmethod
    def search(self, event: FloodEvent) -> list[SearchResult]:
        """Search for available data for the given flood event.

        Args:
            event: The flood event to search for.

        Returns:
            List of search results found.
        """
        ...

    @abstractmethod
    def fetch(self, event: FloodEvent, output_dir: Path) -> list[FetchResult]:
        """Fetch raw data for the given flood event.

        Args:
            event: The flood event to fetch data for.
            output_dir: Directory to save downloaded files.

        Returns:
            List of fetch results with file paths and metadata.
        """
        ...

    def to_dataset(self, result: FetchResult) -> "xr.Dataset":
        """Convert fetch result to xarray Dataset with standard variable names.

        Standard variables:
            - flood_extent: float32, values 0-1
            - quality_mask: uint8, quality flags
            - permanent_water: uint8, permanent water mask

        Args:
            result: The fetch result to convert.

        Returns:
            xarray Dataset with standardised variable names.
        """
        # Default implementation - subclasses should override
        raise NotImplementedError(f"{self.__class__.__name__} does not implement to_dataset")
