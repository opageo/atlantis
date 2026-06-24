"""Archive writer for Zarr storage."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from atlantis.config import HarmoniseConfig
from atlantis.harmoniser.normaliser import Normaliser
from atlantis.models.event import FloodEvent

if TYPE_CHECKING:
    import xarray as xr

#: Spatial dimension candidates (checked in order).
_Y_DIMS = ("y", "lat", "latitude")
_X_DIMS = ("x", "lon", "longitude")

#: Chunk size used for raw data (pixels).
_RAW_CHUNK_SIZE = 256


def _find_dim(dataset: "xr.Dataset", candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if name in dataset.dims:
            return name
    return None


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

        Each event/source combination is written to an independent Zarr store,
        which makes parallel Dask backprocessing safe: workers never write to
        overlapping regions.

        Args:
            dataset: Input xarray Dataset.
            event: Flood event metadata.
            source_id: Data source identifier.

        Returns:
            Path to written Zarr store.

        Raises:
            ValueError: If dataset is empty.
        """
        if not dataset.data_vars:
            raise ValueError("Dataset is empty")

        # 1. Create event/source directory structure
        zarr_path = self.raw_path / event.event_id / source_id / "data.zarr"
        zarr_path.parent.mkdir(parents=True, exist_ok=True)

        # 2. Chunk dataset before writing – each spatial dimension is capped at
        #    _RAW_CHUNK_SIZE so a single chunk never exceeds ~64 MB.
        chunks: dict[str, int] = {}
        y_dim = _find_dim(dataset, _Y_DIMS)
        x_dim = _find_dim(dataset, _X_DIMS)
        if y_dim:
            chunks[y_dim] = min(_RAW_CHUNK_SIZE, dataset.sizes[y_dim])
        if x_dim:
            chunks[x_dim] = min(_RAW_CHUNK_SIZE, dataset.sizes[x_dim])

        ds_chunked = dataset.chunk(chunks) if chunks else dataset

        # 3. Write to native Zarr (overwrites any existing store)
        ds_chunked.to_zarr(zarr_path, mode="w")

        # 4. Write metadata JSON sidecar
        metadata = {
            "event_id": event.event_id,
            "source_id": source_id,
            "bbox": list(event.bbox),
            "start_date": event.start_date.isoformat(),
            "end_date": event.end_date.isoformat(),
            "variables": list(dataset.data_vars),
            "dims": {k: int(v) for k, v in dataset.sizes.items()},
            "crs": dataset.attrs.get("crs", "EPSG:4326"),
            "written_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        metadata_path = zarr_path.parent / "metadata.json"
        with open(metadata_path, "w") as fh:
            json.dump(metadata, fh, indent=2)

        # 5. Return path to Zarr store
        return zarr_path

    def write_ml_ready(
        self,
        dataset: "xr.Dataset",
        event: FloodEvent,
        source_id: str,
        harmonise_config: HarmoniseConfig | None = None,
    ) -> Path:
        """Write ML-ready data to Zarr archive.

        The dataset is normalised, quality-masked, and stored with spatial
        chunks equal to ``tile_size × tile_size`` (default 224 × 224).
        This chunk shape aligns perfectly with ML sample tiles, so loading
        any single tile requires reading exactly one Zarr chunk — the optimal
        access pattern for training data-loaders.

        Writing to native Zarr is safe during parallel Dask backprocessing
        because each (event, source) pair maps to a distinct Zarr store on
        disk; workers therefore never write to the same file concurrently.

        Args:
            dataset: Input xarray Dataset.
            event: Flood event metadata.
            source_id: Data source identifier.
            harmonise_config: Harmonisation configuration used.

        Returns:
            Path to written Zarr store.
        """
        cfg = harmonise_config or HarmoniseConfig()
        tile_size = cfg.tile_size

        # 1. Apply normalisation to flood_fraction
        normaliser = Normaliser()
        if "flood_fraction" in dataset.data_vars:
            dataset = normaliser.normalise(dataset, variable="flood_fraction")

        # 2. Generate quality mask (if not already present)
        if "quality_mask" not in dataset.data_vars:
            flood_var = "flood_fraction" if "flood_fraction" in dataset.data_vars else list(dataset.data_vars)[0]
            dataset = dataset.assign(quality_mask=normaliser.generate_quality_mask(dataset, variable=flood_var))

        # 3. Generate permanent water mask (if not already present)
        if "permanent_water" not in dataset.data_vars:
            dataset = dataset.assign(permanent_water=normaliser.generate_permanent_water_mask(dataset))

        # 4. Create directory and write to ML-ready Zarr with tile-aligned chunks
        zarr_path = self.ml_path / event.event_id / source_id / "data.zarr"
        zarr_path.parent.mkdir(parents=True, exist_ok=True)

        chunks: dict[str, int] = {}
        y_dim = _find_dim(dataset, _Y_DIMS)
        x_dim = _find_dim(dataset, _X_DIMS)
        if y_dim:
            chunks[y_dim] = min(tile_size, dataset.sizes[y_dim])
        if x_dim:
            chunks[x_dim] = min(tile_size, dataset.sizes[x_dim])

        ds_chunked = dataset.chunk(chunks) if chunks else dataset
        ds_chunked.to_zarr(zarr_path, mode="w")

        # 5. Write metadata and config sidecar
        metadata: dict = {
            "event_id": event.event_id,
            "source_id": source_id,
            "bbox": list(event.bbox),
            "start_date": event.start_date.isoformat(),
            "end_date": event.end_date.isoformat(),
            "variables": list(dataset.data_vars),
            "dims": {k: int(v) for k, v in dataset.sizes.items()},
            "tile_size": tile_size,
            "crs": dataset.attrs.get("crs", "EPSG:4326"),
            "harmonise_config": cfg.model_dump(),
            "written_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        metadata_path = zarr_path.parent / "metadata.json"
        with open(metadata_path, "w") as fh:
            json.dump(metadata, fh, indent=2)

        return zarr_path

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
