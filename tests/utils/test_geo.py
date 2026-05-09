"""Tests for geo utility functions."""

import pytest

from atlantis.utils.geo import (
    BBox,
    bbox_area,
    bbox_intersects,
    tile_bbox,
    validate_bbox,
)


class TestValidateBBox:
    """Tests for validate_bbox function."""

    def test_valid_bbox_tuple(self):
        """Test validation of valid bbox tuple."""
        assert validate_bbox((0, 0, 1, 1)) is True
        assert validate_bbox((-10, -20, 10, 20)) is True
        assert validate_bbox((-180, -90, 180, 90)) is True

    def test_valid_bbox_object(self):
        """Test validation of valid BBox object."""
        bbox = BBox(west=0, south=0, east=1, north=1)
        assert validate_bbox(bbox) is True

    def test_invalid_longitude(self):
        """Test that longitude outside -180 to 180 is invalid."""
        assert validate_bbox((-200, 0, 1, 1)) is False
        assert validate_bbox((0, 0, 200, 1)) is False

    def test_invalid_latitude(self):
        """Test that latitude outside -90 to 90 is invalid."""
        assert validate_bbox((0, -100, 1, 1)) is False
        assert validate_bbox((0, 0, 1, 100)) is False

    def test_west_greater_than_east(self):
        """Test that west > east is invalid."""
        assert validate_bbox((1, 0, 0, 1)) is False

    def test_south_greater_than_north(self):
        """Test that south > north is invalid."""
        assert validate_bbox((0, 1, 1, 0)) is False


class TestBBoxIntersects:
    """Tests for bbox_intersects function."""

    def test_overlapping_boxes(self):
        """Test that overlapping boxes return True."""
        assert bbox_intersects((0, 0, 1, 1), (0.5, 0.5, 1.5, 1.5)) is True
        assert bbox_intersects((0, 0, 10, 10), (5, 5, 15, 15)) is True

    def test_non_overlapping_boxes(self):
        """Test that non-overlapping boxes return False."""
        assert bbox_intersects((0, 0, 1, 1), (2, 2, 3, 3)) is False
        assert bbox_intersects((0, 0, 1, 1), (-3, -3, -2, -2)) is False

    def test_adjacent_boxes(self):
        """Test that adjacent boxes with shared edges intersect."""
        # Boxes sharing edges are considered intersecting in this implementation
        assert bbox_intersects((0, 0, 1, 1), (1, 0, 2, 1)) is True
        assert bbox_intersects((0, 0, 1, 1), (0, 1, 1, 2)) is True

    def test_contained_box(self):
        """Test that contained boxes intersect."""
        assert bbox_intersects((0, 0, 10, 10), (3, 3, 5, 5)) is True
        assert bbox_intersects((3, 3, 5, 5), (0, 0, 10, 10)) is True


class TestBBoxArea:
    """Tests for bbox_area function."""

    def test_square_box(self):
        """Test area of square bbox."""
        area = bbox_area((0, 0, 10, 10))
        assert area == 100

    def test_rectangular_box(self):
        """Test area of rectangular bbox."""
        area = bbox_area((0, 0, 5, 10))
        assert area == 50


class TestTileBBox:
    """Tests for tile_bbox function."""

    def test_single_tile(self):
        """Test tile bbox for 1x1 grid."""
        bbox = tile_bbox((0, 0, 10, 10), rows=1, cols=1, row=0, col=0)
        assert bbox == (0, 0, 10, 10)

    def test_2x2_grid(self):
        """Test tile bboxes for 2x2 grid (row-major order, y from south to north)."""
        # Row 0, Col 0: west=0, south=0, east=5, north=5
        bbox_tl = tile_bbox((0, 0, 10, 10), rows=2, cols=2, row=0, col=0)
        assert bbox_tl == (0.0, 0.0, 5.0, 5.0)

        # Row 0, Col 1: west=5, south=0, east=10, north=5
        bbox_tr = tile_bbox((0, 0, 10, 10), rows=2, cols=2, row=0, col=1)
        assert bbox_tr == (5.0, 0.0, 10.0, 5.0)

        # Row 1, Col 0: west=0, south=5, east=5, north=10
        bbox_bl = tile_bbox((0, 0, 10, 10), rows=2, cols=2, row=1, col=0)
        assert bbox_bl == (0.0, 5.0, 5.0, 10.0)

        # Row 1, Col 1: west=5, south=5, east=10, north=10
        bbox_br = tile_bbox((0, 0, 10, 10), rows=2, cols=2, row=1, col=1)
        assert bbox_br == (5.0, 5.0, 10.0, 10.0)

    def test_out_of_bounds(self):
        """Test that out of bounds raises IndexError."""
        with pytest.raises(IndexError, match="out of bounds"):
            tile_bbox((0, 0, 10, 10), rows=2, cols=2, row=2, col=0)

        with pytest.raises(IndexError, match="out of bounds"):
            tile_bbox((0, 0, 10, 10), rows=2, cols=2, row=0, col=2)
