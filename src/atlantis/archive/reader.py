"""Archive reader for Zarr/STAC access."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import xarray as xr

#: Spatial dimension candidates (checked in order).
_Y_DIMS = ("y", "lat", "latitude")
_X_DIMS = ("x", "lon", "longitude")


def _find_dim(dataset: "xr.Dataset", candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if name in dataset.dims:
            return name
    return None


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
            Raw xarray Dataset (lazily loaded).

        Raises:
            FileNotFoundError: If archive doesn't exist.
        """
        import xarray as xr

        zarr_path = self.archive_root / "raw" / event_id / source_id / "data.zarr"
        if not zarr_path.exists():
            raise FileNotFoundError(f"Raw archive not found: {zarr_path}")
        return xr.open_zarr(zarr_path)

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
                   Each (row, col) selects a ``tile_size × tile_size`` window
                   from the spatial grid.  Pass ``None`` to read everything.

        Returns:
            ML-ready xarray Dataset (lazily loaded).

        Raises:
            FileNotFoundError: If archive doesn't exist.
        """
        import xarray as xr

        zarr_path = self.archive_root / "ml-ready" / event_id / source_id / "data.zarr"
        if not zarr_path.exists():
            raise FileNotFoundError(f"ML-ready archive not found: {zarr_path}")

        ds = xr.open_zarr(zarr_path)

        if tiles is None:
            return ds

        # Read tile_size from the metadata sidecar (fall back to 224).
        tile_size = self._read_tile_size(event_id, source_id)

        y_dim = _find_dim(ds, _Y_DIMS)
        x_dim = _find_dim(ds, _X_DIMS)
        if y_dim is None or x_dim is None:
            return ds

        height = ds.sizes[y_dim]
        width = ds.sizes[x_dim]

        # Compute the bounding pixel ranges that cover all requested tiles.
        # Using contiguous slices is far more memory-efficient than
        # collecting individual pixel indices, and works well for the typical
        # case where ML data-loaders request spatially adjacent tile windows.
        y_min = height
        y_max = 0
        x_min = width
        x_max = 0
        for row, col in tiles:
            y_start = row * tile_size
            y_end = min(y_start + tile_size, height)
            x_start = col * tile_size
            x_end = min(x_start + tile_size, width)
            y_min = min(y_min, y_start)
            y_max = max(y_max, y_end)
            x_min = min(x_min, x_start)
            x_max = max(x_max, x_end)

        return ds.isel(**{y_dim: slice(y_min, y_max), x_dim: slice(x_min, x_max)})

    def list_events(self) -> list[str]:
        """List all available events in the archive.

        Returns:
            Sorted list of event IDs present in either the raw or ml-ready
            sub-trees.
        """
        events: set[str] = set()
        for subdir in ("raw", "ml-ready"):
            directory = self.archive_root / subdir
            if directory.exists():
                events.update(entry.name for entry in directory.iterdir() if entry.is_dir())
        return sorted(events)

    def list_sources(self, event_id: str) -> list[str]:
        """List all available sources for an event.

        Args:
            event_id: Flood event identifier.

        Returns:
            Sorted list of source IDs present in either the raw or ml-ready
            sub-trees for this event.
        """
        sources: set[str] = set()
        for subdir in ("raw", "ml-ready"):
            directory = self.archive_root / subdir / event_id
            if directory.exists():
                sources.update(entry.name for entry in directory.iterdir() if entry.is_dir())
        return sorted(sources)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _read_tile_size(self, event_id: str, source_id: str) -> int:
        """Read tile_size from the ML-ready metadata sidecar (default 224)."""
        metadata_path = self.archive_root / "ml-ready" / event_id / source_id / "metadata.json"
        if metadata_path.exists():
            with open(metadata_path) as fh:
                meta = json.load(fh)
            return int(meta.get("tile_size", 224))
        return 224
