"""Geographic utility functions."""

from dataclasses import dataclass


@dataclass
class BBox:
    """Bounding box in geographic coordinates.

    Attributes:
        west: Western longitude (min x).
        south: Southern latitude (min y).
        east: Eastern longitude (max x).
        north: Northern latitude (max y).
    """

    west: float
    south: float
    east: float
    north: float

    def __post_init__(self) -> None:
        """Validate bbox coordinates."""
        if not validate_bbox(self):
            raise ValueError(
                f"Invalid bbox: west={self.west}, south={self.south}, east={self.east}, north={self.north}"
            )


def validate_bbox(bbox: tuple[float, float, float, float] | BBox) -> bool:
    """Validate a bounding box.

    Args:
        bbox: Bounding box as (west, south, east, north) tuple or BBox.

    Returns:
        True if bbox is valid, False otherwise.
    """
    if isinstance(bbox, BBox):
        west, south, east, north = bbox.west, bbox.south, bbox.east, bbox.north
    else:
        west, south, east, north = bbox

    # Check longitude range
    if not (-180 <= west <= 180 and -180 <= east <= 180):
        return False

    # Check latitude range
    if not (-90 <= south <= 90 and -90 <= north <= 90):
        return False

    # Check order
    if west > east or south > north:
        return False

    return True


def bbox_intersects(
    bbox1: tuple[float, float, float, float],
    bbox2: tuple[float, float, float, float],
) -> bool:
    """Check if two bounding boxes intersect.

    Args:
        bbox1: First bounding box (west, south, east, north).
        bbox2: Second bounding box (west, south, east, north).

    Returns:
        True if bounding boxes intersect, False otherwise.
    """
    w1, s1, e1, n1 = bbox1
    w2, s2, e2, n2 = bbox2

    # Check for no intersection
    if e1 < w2 or e2 < w1:  # One box is entirely to the left
        return False
    if n1 < s2 or n2 < s1:  # One box is entirely below
        return False

    return True


def bbox_area(bbox: tuple[float, float, float, float]) -> float:
    """Calculate approximate area of a bounding box in square degrees.

    Args:
        bbox: Bounding box (west, south, east, north).

    Returns:
        Area in square degrees.
    """
    west, south, east, north = bbox
    width = abs(east - west)
    height = abs(north - south)
    return width * height


def tile_bbox(
    bbox: tuple[float, float, float, float],
    rows: int,
    cols: int,
    row: int,
    col: int,
) -> tuple[float, float, float, float]:
    """Calculate the bounding box for a specific tile in a grid.

    Args:
        bbox: Overall bounding box (west, south, east, north).
        rows: Number of rows in the grid.
        cols: Number of columns in the grid.
        row: Row index (0-based).
        col: Column index (0-based).

    Returns:
        Bounding box for the specified tile.

    Raises:
        IndexError: If row/col are out of bounds.
    """
    if row < 0 or row >= rows:
        raise IndexError(f"Row {row} out of bounds [0, {rows})")
    if col < 0 or col >= cols:
        raise IndexError(f"Col {col} out of bounds [0, {cols})")

    west, south, east, north = bbox
    width = (east - west) / cols
    height = (north - south) / rows

    tile_west = west + col * width
    tile_east = tile_west + width
    tile_south = south + row * height
    tile_north = tile_south + height

    return (tile_west, tile_south, tile_east, tile_north)
