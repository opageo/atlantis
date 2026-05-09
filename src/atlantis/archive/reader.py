"""Archive reader for Zarr/STAC access."""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import xarray as xr


class ArchiveReader:
    """Reads flood data from Zarr archives."""

    def __init__(self, archive_root: Path) -> None:
        """Initialize the archive reader.

        Args:
            archive_root: Root directory for archive storage.
        """
        self.archive_root = Path(archive_root)

    def read_raw(
        self,
        event_id: str,
        source_id: str,
    ) -> "xr.Dataset":
        """Read raw data from Zarr archive.

        Args:
            event_id: Flood event identifier.
            source_id: Data source identifier.

        Returns:
            Raw xarray Dataset.

        Raises:
            FileNotFoundError: If archive doesn't exist.
        """
        # TODO: Implement raw archive reading
        # Expected implementation:
        # 1. Construct path to Zarr store
        # 2. Open with xarray.open_zarr
        # 3. Return dataset
        raise NotImplementedError("Raw archive reading not yet implemented")

    def read_ml_ready(
        self,
        event_id: str,
        source_id: str,
        tiles: list[tuple[int, int]] | None = None,
    ) -> "xr.Dataset":
        """Read ML-ready data from Zarr archive.

        Args:
            event_id: Flood event identifier.
            source_id: Data source identifier.
            tiles: Optional list of (row, col) tile indices to read.

        Returns:
            ML-ready xarray Dataset.

        Raises:
            FileNotFoundError: If archive doesn't exist.
        """
        # TODO: Implement ML-ready archive reading
        # Expected implementation:
        # 1. Construct path to ML Zarr store
        # 2. Optionally filter by tile indices
        # 3. Open with xarray.open_zarr
        # 4. Return dataset
        raise NotImplementedError("ML-ready archive reading not yet implemented")

    def list_events(self) -> list[str]:
        """List all available events in the archive.

        Returns:
            List of event IDs.
        """
        # TODO: Implement event listing
        raise NotImplementedError("Event listing not yet implemented")

    def list_sources(self, event_id: str) -> list[str]:
        """List all available sources for an event.

        Args:
            event_id: Flood event identifier.

        Returns:
            List of source IDs.
        """
        # TODO: Implement source listing
        raise NotImplementedError("Source listing not yet implemented")
