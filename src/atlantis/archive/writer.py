"""Archive writer for Zarr storage."""

from pathlib import Path
from typing import TYPE_CHECKING

from atlantis.models.event import FloodEvent

if TYPE_CHECKING:
    import xarray as xr


class ArchiveWriter:
    """Writes flood data to Zarr archives.

    Supports both raw (unprocessed) and ML-ready (harmonised) archives.
    """

    def __init__(self, archive_root: Path) -> None:
        """Initialize the archive writer.

        Args:
            archive_root: Root directory for archive storage.
        """
        self.archive_root = Path(archive_root)
        self.raw_path = self.archive_root / "raw"
        self.ml_path = self.archive_root / "ml-ready"

    def write_raw(
        self,
        dataset: "xr.Dataset",
        event: FloodEvent,
        source_id: str,
    ) -> Path:
        """Write raw data to Zarr archive.

        Args:
            dataset: Input xarray Dataset.
            event: Flood event metadata.
            source_id: Data source identifier.

        Returns:
            Path to written Zarr store.

        Raises:
            ValueError: If dataset is empty.
        """
        # TODO: Implement raw archive writing
        # Expected implementation:
        # 1. Create event/source directory structure
        # 2. Open or create Zarr group
        # 3. Write dataset with compression
        # 4. Write metadata JSON sidecar
        # 5. Return path to Zarr store
        raise NotImplementedError("Raw archive writing not yet implemented")

    def write_ml_ready(
        self,
        dataset: "xr.Dataset",
        event: FloodEvent,
        source_id: str,
        harmonise_config: dict | None = None,
    ) -> Path:
        """Write ML-ready data to Zarr archive.

        ML-ready data is tiled, normalised, and includes quality masks.

        Args:
            dataset: Input xarray Dataset.
            event: Flood event metadata.
            source_id: Data source identifier.
            harmonise_config: Harmonisation configuration used.

        Returns:
            Path to written Zarr store.
        """
        # TODO: Implement ML-ready archive writing
        # Expected implementation:
        # 1. Apply tiling (224x224 tiles)
        # 2. Apply normalisation
        # 3. Generate quality masks
        # 4. Write to ML-ready Zarr with proper chunking
        # 5. Write metadata and config
        raise NotImplementedError("ML-ready archive writing not yet implemented")

    def write_checkpoint(
        self,
        event: FloodEvent,
        source_id: str,
        stage: str,
    ) -> Path:
        """Write a checkpoint marker for resumed processing.

        Args:
            event: Flood event metadata.
            source_id: Data source identifier.
            stage: Processing stage (fetch, harmonise, archive).

        Returns:
            Path to checkpoint marker file.
        """
        checkpoint_dir = self.archive_root / ".checkpoints" / event.event_id
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = checkpoint_dir / f"{source_id}_{stage}.done"
        checkpoint_path.touch()
        return checkpoint_path

    def is_checkpointed(
        self,
        event: FloodEvent,
        source_id: str,
        stage: str,
    ) -> bool:
        """Check if a processing stage is checkpointed.

        Args:
            event: Flood event metadata.
            source_id: Data source identifier.
            stage: Processing stage.

        Returns:
            True if checkpoint exists, False otherwise.
        """
        checkpoint_path = self.archive_root / ".checkpoints" / event.event_id / f"{source_id}_{stage}.done"
        return checkpoint_path.exists()
