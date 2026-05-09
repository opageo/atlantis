"""Tiler for creating uniform grid tiles from flood data."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import xarray as xr


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
        self.tile_size = tile_size
        self.overlap = overlap

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
        # TODO: Implement tiling logic
        # Expected implementation:
        # 1. Determine grid dimensions based on tile_size
        # 2. Create overlapping or non-overlapping tiles
        # 3. Extract each tile as a new Dataset
        # 4. Return list of tiles with metadata
        raise NotImplementedError("Tiling not yet implemented")

    def count_tiles(self, dataset: "xr.Dataset") -> tuple[int, int]:
        """Count tiles needed to cover the dataset.

        Args:
            dataset: Input xarray Dataset.

        Returns:
            Tuple of (n_tiles_row, n_tiles_col) or (n_tiles_y, n_tiles_x).
        """
        # TODO: Implement tile counting
        raise NotImplementedError("Tile counting not yet implemented")

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
        # TODO: Implement bbox calculation
        raise NotImplementedError("Tile bbox calculation not yet implemented")
