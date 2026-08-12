"""Tests for the canonical global grid and AOI → index mapping."""

import numpy as np
import pytest

from atlantis.archive import grid


class TestGlobalCoords:
    def test_shapes(self):
        assert grid.global_x_coords().shape == (grid.GLOBAL_WIDTH,)
        assert grid.global_y_coords().shape == (grid.GLOBAL_HEIGHT,)

    def test_pixel_centre_convention(self):
        x = grid.global_x_coords()
        y = grid.global_y_coords()
        # First pixel centres sit half a pixel inside the origin edges.
        assert x[0] == pytest.approx(-180.0 + 0.5 / 60.0)
        assert y[0] == pytest.approx(90.0 - 0.5 / 60.0)
        # Monotonic: lon ascending, lat descending.
        assert x[1] > x[0]
        assert y[1] < y[0]


class TestSnap:
    def test_matches_reprojector(self):
        """grid.snap_bounds must reproduce the harmoniser's snapping exactly."""
        from atlantis.harmoniser.reprojector import Reprojector

        reproj = Reprojector()
        bounds = (-1.5, 38.8, 0.5, 40.0)
        assert grid.snap_bounds(*bounds) == reproj._snap_bounds_to_global_grid(*bounds)


class TestWindow:
    def test_bounds_to_window_roundtrip(self):
        window = grid.bounds_to_window(-1.5, 38.8, 0.5, 40.0)
        assert window.width > 0 and window.height > 0
        # Coordinates carved from the global axes must map back to the window.
        y = grid.global_y_coords()[window.row_start : window.row_stop]
        x = grid.global_x_coords()[window.col_start : window.col_stop]
        assert grid.coords_to_window(y, x) == window

    def test_coords_to_window_rejects_unaligned(self):
        with pytest.raises(ValueError, match="not aligned"):
            grid.coords_to_window(np.array([0.0, 0.5, 1.0]), np.array([0.0, 0.5, 1.0]))

    def test_index_window_out_of_range(self):
        with pytest.raises(ValueError):
            grid.IndexWindow(-1, 5, 0, 5)
        with pytest.raises(ValueError):
            grid.IndexWindow(0, 5, 0, grid.GLOBAL_WIDTH + 1)


class TestAoiBlocks:
    def test_single_interior_block_is_512x512(self):
        blocks = grid.aoi_blocks(-5.0, 40.0, -1.5, 45.0)
        assert len(blocks) == 1
        block = blocks[0]
        assert (block.height, block.width) == (512, 512)
        assert (block.row_start, block.col_start) == (2560, 10240)

    def test_straddling_bbox_spans_two_blocks(self):
        blocks = grid.aoi_blocks(-1.5, 38.8, 0.5, 40.0)
        assert len(blocks) == 2
        assert blocks[0].col_start == 10240  # west block
        assert blocks[1].col_start == 10752  # east block
        assert blocks[0].row_start == blocks[1].row_start

    def test_southern_edge_block_is_trimmed(self):
        blocks = grid.aoi_blocks(-5.0, -90.0, -1.5, -88.0)
        assert len(blocks) == 2
        assert blocks[-1].row_stop == grid.GLOBAL_HEIGHT
        assert blocks[-1].height == grid.GLOBAL_HEIGHT - 10752

    def test_blocks_align_to_global_grid(self):
        for block in grid.aoi_blocks(-10.0, 20.0, 15.0, 55.0):
            y = grid.global_y_coords()[block.row_start : block.row_stop]
            x = grid.global_x_coords()[block.col_start : block.col_stop]
            assert grid.coords_to_window(y, x) == block

    def test_block_id_is_stable(self):
        block = grid.aoi_blocks(-5.0, 40.0, -1.5, 45.0)[0]
        assert grid.aoi_block_id(block) == "a512_r05c020"

    def test_rejects_nonpositive_size(self):
        with pytest.raises(ValueError, match="block_size"):
            grid.aoi_blocks(-1.0, 0.0, 1.0, 2.0, block_size=0)
