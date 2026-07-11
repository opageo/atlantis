"""Tiler for creating uniform grid tiles from flood data."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import xarray as xr

#: Spatial dimension names tried in order (y-axis then x-axis).
_Y_DIMS = ("y", "lat", "latitude")
_X_DIMS = ("x", "lon", "longitude")


def _find_dim(dataset: "xr.Dataset", candidates: tuple[str, ...]) -> str | None:
    """Return the first candidate that is a dimension of *dataset*, or None."""
    for name in candidates:
        if name in dataset.dims:
            return name
    return None


def _pixel_resolution(coord_values: np.ndarray, fallback: float = 0.0) -> float:
    """Estimate pixel size from a 1-D coordinate array."""
    if len(coord_values) > 1:
        return abs(float(coord_values[1] - coord_values[0]))
    return fallback


class Tiler:
    """Creates uniform grid tiles from irregular flood data.

    Attributes:
        tile_size: Size of square tiles in pixels (e.g., 224 for ML models).
        overlap: Overlap between adjacent tiles in pixels.
    """

    def __init__(self, tile_size: int = 224, overlap: int = 0) -> None:
        """Initialize the tiler.

        Args:
            tile_size: Size of square tiles in pixels.
            overlap: Overlap between adjacent tiles.
        """
        if tile_size <= 0:
            raise ValueError("tile_size must be positive")
        if overlap < 0:
            raise ValueError("overlap must be non-negative")
        if overlap >= tile_size:
            raise ValueError("overlap must be less than tile_size")
        self.tile_size = tile_size
        self.overlap = overlap

    # ── Public API ────────────────────────────────────────────────────────

    def tile_dataset(self, dataset: "xr.Dataset") -> list[tuple["xr.Dataset", dict]]:
        """Tile a dataset into uniform chunks.

        Args:
            dataset: Input xarray Dataset to tile.

        Returns:
            List of (tile_dataset, tile_metadata) tuples.
            Tile metadata includes: row, col, bbox, valid_pixels.

        Raises:
            ValueError: If dataset has no spatial dimensions.
        """
        y_dim = _find_dim(dataset, _Y_DIMS)
        x_dim = _find_dim(dataset, _X_DIMS)
        if y_dim is None or x_dim is None:
            raise ValueError(
                "Dataset must have recognisable spatial dimensions "
                f"(y: {_Y_DIMS}, x: {_X_DIMS}). Found dims: {list(dataset.dims)}"
            )

        height = dataset.sizes[y_dim]
        width = dataset.sizes[x_dim]
        stride = self.tile_size - self.overlap

        # Pre-compute pixel resolutions for bbox calculation.
        if y_dim in dataset.coords:
            full_y = dataset.coords[y_dim].values
            res_y = _pixel_resolution(full_y)
        else:
            res_y = 0.0

        if x_dim in dataset.coords:
            full_x = dataset.coords[x_dim].values
            res_x = _pixel_resolution(full_x)
        else:
            res_x = 0.0

        tiles: list[tuple[xr.Dataset, dict]] = []
        row_idx = 0
        for y_start in range(0, height, stride):
            y_end = min(y_start + self.tile_size, height)
            col_idx = 0
            for x_start in range(0, width, stride):
                x_end = min(x_start + self.tile_size, width)

                tile_ds = dataset.isel(**{y_dim: slice(y_start, y_end), x_dim: slice(x_start, x_end)})

                bbox = self._compute_bbox(tile_ds, y_dim, x_dim, res_y, res_x, y_start, y_end, x_start, x_end)
                valid_pixels = self._count_valid_pixels(tile_ds)

                metadata: dict = {
                    "row": row_idx,
                    "col": col_idx,
                    "bbox": bbox,
                    "valid_pixels": valid_pixels,
                }
                tiles.append((tile_ds, metadata))
                col_idx += 1
            row_idx += 1

        return tiles

    def count_tiles(self, dataset: "xr.Dataset") -> tuple[int, int]:
        """Count tiles needed to cover the dataset.

        Args:
            dataset: Input xarray Dataset.

        Returns:
            Tuple of (n_tiles_row, n_tiles_col).
        """
        y_dim = _find_dim(dataset, _Y_DIMS)
        x_dim = _find_dim(dataset, _X_DIMS)
        stride = self.tile_size - self.overlap
        height = dataset.sizes.get(y_dim, 0) if y_dim else 0
        width = dataset.sizes.get(x_dim, 0) if x_dim else 0
        n_rows = max(1, int(np.ceil(height / stride))) if height > 0 else 0
        n_cols = max(1, int(np.ceil(width / stride))) if width > 0 else 0
        return (n_rows, n_cols)

    def get_tile_bbox(
        self,
        row: int,
        col: int,
        dataset: "xr.Dataset",
    ) -> tuple[float, float, float, float]:
        """Get the bounding box for a specific tile.

        Args:
            row: Tile row index.
            col: Tile column index.
            dataset: Input dataset for spatial reference.

        Returns:
            Bounding box as (west, south, east, north).

        Raises:
            IndexError: If row/col are out of bounds.
        """
        n_rows, n_cols = self.count_tiles(dataset)
        if row < 0 or row >= n_rows:
            raise IndexError(f"row {row} out of range [0, {n_rows})")
        if col < 0 or col >= n_cols:
            raise IndexError(f"col {col} out of range [0, {n_cols})")

        y_dim = _find_dim(dataset, _Y_DIMS)
        x_dim = _find_dim(dataset, _X_DIMS)
        stride = self.tile_size - self.overlap
        height = dataset.sizes[y_dim]
        width = dataset.sizes[x_dim]

        y_start = row * stride
        y_end = min(y_start + self.tile_size, height)
        x_start = col * stride
        x_end = min(x_start + self.tile_size, width)

        tile_ds = dataset.isel(**{y_dim: slice(y_start, y_end), x_dim: slice(x_start, x_end)})

        if y_dim in dataset.coords:
            full_y = dataset.coords[y_dim].values
            res_y = _pixel_resolution(full_y)
        else:
            res_y = 0.0
        if x_dim in dataset.coords:
            full_x = dataset.coords[x_dim].values
            res_x = _pixel_resolution(full_x)
        else:
            res_x = 0.0

        return self._compute_bbox(tile_ds, y_dim, x_dim, res_y, res_x, y_start, y_end, x_start, x_end)

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _compute_bbox(
        tile_ds: "xr.Dataset",
        y_dim: str,
        x_dim: str,
        res_y: float,
        res_x: float,
        y_start: int,
        y_end: int,
        x_start: int,
        x_end: int,
    ) -> tuple[float, float, float, float]:
        """Compute (west, south, east, north) pixel-edge bounds for a tile."""
        if y_dim in tile_ds.coords and x_dim in tile_ds.coords:
            y_vals = tile_ds.coords[y_dim].values
            x_vals = tile_ds.coords[x_dim].values
            half_y = res_y / 2.0
            half_x = res_x / 2.0
            west = float(x_vals.min()) - half_x
            east = float(x_vals.max()) + half_x
            south = float(y_vals.min()) - half_y
            north = float(y_vals.max()) + half_y
        else:
            # Fall back to pixel-index coordinates
            west = float(x_start)
            east = float(x_end)
            south = float(y_start)
            north = float(y_end)
        return (west, south, east, north)

    @staticmethod
    def _count_valid_pixels(tile_ds: "xr.Dataset") -> int:
        """Count non-NaN pixels in the first floating-point data variable of a tile.

        Uses the first float variable as a proxy for data coverage; non-float
        variables (e.g. masks) are treated as fully valid.
        """
        for var in tile_ds.data_vars:
            arr = tile_ds[var].values
            if np.issubdtype(arr.dtype, np.floating):
                return int(np.sum(~np.isnan(arr)))
            return int(arr.size)
        return 0
