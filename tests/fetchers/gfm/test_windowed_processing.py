"""Unit tests for the pure GFM windowed-processing helpers
(``plan-gfmWindowedMemoryFix.prompt.md``, Phase W.2/W.5).

These are the pure, network-free building blocks for windowed native
processing: `_native_pixel_windows` (window tiling), `_window_native_bounds`
/ `_window_bbox_4326` (pixel-window -> bbox coordinate math), and
`_crop_to_native_bounds` (exact post-load crop). Verified correct in
isolation against real STAC data during the Phase W.2 investigation (see
"Phase W.2 correctness gate" in /memories/repo/gfm-investigation.md) — the
still-open correctness gap for the *full pipeline* (`window_size` on
`GfmRasterProcessor`) is downstream of these helpers, not in them.
"""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from atlantis.fetchers.gfm.processor import (
    _crop_to_native_bounds,
    _native_pixel_windows,
    _window_bbox_4326,
    _window_native_bounds,
)

# A representative real GFM Equi7 transform (EU020M_E036N009T3, confirmed via
# direct STAC query — see /memories/repo/gfm-investigation.md "Phase W.1
# catalogue sampling"): 20 m pixels, north-up, axis-aligned.
_TRANSFORM = (20.0, 0.0, 3_600_000.0, 0.0, -20.0, 1_200_000.0)
_WKT2 = (
    'PROJCS["Azimuthal_Equidistant",GEOGCS["WGS 84",DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],'
    'UNIT["degree",0.0174532925199433]],PROJECTION["Azimuthal_Equidistant"],'
    'PARAMETER["latitude_of_center",53],PARAMETER["longitude_of_center",24],'
    'PARAMETER["false_easting",5837287.81977],'
    'PARAMETER["false_northing",2121415.696777],UNIT["metre",1]]'
)


class TestNativePixelWindows:
    def test_zero_phase_covers_full_grid_no_gaps_no_overlaps(self) -> None:
        shape = (16, 16)
        windows = _native_pixel_windows(shape, window_size=4, coarsen_factor=4)

        covered = np.zeros(shape, dtype=int)
        for row_start, row_end, col_start, col_end in windows:
            covered[row_start:row_end, col_start:col_end] += 1

        assert covered.min() == 1
        assert covered.max() == 1  # no overlaps
        assert covered.sum() == shape[0] * shape[1]  # no gaps

    def test_zero_phase_last_window_trimmed_at_true_outer_edge_only(self) -> None:
        # 10 doesn't divide evenly by window_size=4 -> last window is a
        # smaller (2px) trailing remainder, trimmed only at the true edge.
        shape = (10, 10)
        windows = _native_pixel_windows(shape, window_size=4, coarsen_factor=2)
        covered = np.zeros(shape, dtype=int)
        for row_start, row_end, col_start, col_end in windows:
            covered[row_start:row_end, col_start:col_end] += 1
        assert covered.min() == 1
        assert covered.max() == 1
        assert covered.sum() == 100

    def test_nonzero_phase_covers_every_real_pixel_exactly_once(self) -> None:
        """Windows may extend below 0 / beyond shape (phantom edge pixels,
        mirroring the unwindowed reference's own out-of-tile nodata
        inclusion — see `_native_pixel_windows` docstring), but every REAL
        pixel index must still be covered exactly once."""
        shape = (23, 19)
        for phase in [(0, 0), (1, 2), (2, 3), (3, 1)]:
            windows = _native_pixel_windows(shape, window_size=8, coarsen_factor=4, phase=phase)
            covered = np.zeros(shape, dtype=int)
            for row_start, row_end, col_start, col_end in windows:
                real_row_start, real_row_end = max(row_start, 0), min(row_end, shape[0])
                real_col_start, real_col_end = max(col_start, 0), min(col_end, shape[1])
                covered[real_row_start:real_row_end, real_col_start:real_col_end] += 1
            assert covered.min() == 1, f"gap found for phase={phase}"
            assert covered.max() == 1, f"overlap found for phase={phase}"

    def test_nonzero_phase_windows_are_coarsen_factor_multiples(self) -> None:
        shape = (37, 41)
        coarsen_factor = 4
        for phase in [(1, 0), (2, 3), (3, 2)]:
            windows = _native_pixel_windows(shape, window_size=8, coarsen_factor=coarsen_factor, phase=phase)
            for row_start, row_end, col_start, col_end in windows:
                assert (row_end - row_start) % coarsen_factor == 0
                assert (col_end - col_start) % coarsen_factor == 0

    def test_raises_on_window_size_not_multiple_of_coarsen_factor(self) -> None:
        with pytest.raises(ValueError, match="exact multiple"):
            _native_pixel_windows((100, 100), window_size=15, coarsen_factor=4)

    def test_raises_on_nonpositive_window_size(self) -> None:
        with pytest.raises(ValueError):
            _native_pixel_windows((100, 100), window_size=0, coarsen_factor=4)

    def test_raises_on_invalid_coarsen_factor(self) -> None:
        with pytest.raises(ValueError):
            _native_pixel_windows((100, 100), window_size=8, coarsen_factor=0)


class TestWindowNativeBounds:
    def test_pixel_center_bounds_match_transform(self) -> None:
        # window covering native pixel rows [10, 14), cols [20, 24)
        bounds = _window_native_bounds((10, 14, 20, 24), _TRANSFORM)
        a, _b, c, _d, e, f = _TRANSFORM
        expected_x0 = c + (20 + 0.5) * a
        expected_x1 = c + (24 - 0.5) * a
        expected_y0 = f + (10 + 0.5) * e
        expected_y1 = f + (14 - 0.5) * e
        x_min, y_min, x_max, y_max = bounds
        assert x_min == pytest.approx(min(expected_x0, expected_x1))
        assert x_max == pytest.approx(max(expected_x0, expected_x1))
        assert y_min == pytest.approx(min(expected_y0, expected_y1))
        assert y_max == pytest.approx(max(expected_y0, expected_y1))

    def test_single_pixel_window(self) -> None:
        x_min, y_min, x_max, y_max = _window_native_bounds((5, 6, 7, 8), _TRANSFORM)
        assert x_max - x_min == pytest.approx(0.0)
        assert y_max - y_min == pytest.approx(0.0)

    def test_rejects_rotated_transform(self) -> None:
        rotated = (20.0, 5.0, 3_600_000.0, 5.0, -20.0, 1_200_000.0)
        with pytest.raises(ValueError, match="axis-aligned"):
            _window_native_bounds((0, 4, 0, 4), rotated)


class TestWindowBbox4326:
    def test_encloses_the_unpadded_window_bounds(self) -> None:
        """R.1 mitigation: the returned lon/lat bbox must fully enclose the
        window's exact native bounds (never under-cover it), even though the
        native CRS is not conformal to lon/lat."""
        import pyproj

        window = (100, 200, 300, 400)
        bbox = _window_bbox_4326(window, _TRANSFORM, _WKT2, margin_px=2)
        west, south, east, north = bbox

        x_min, y_min, x_max, y_max = _window_native_bounds(window, _TRANSFORM)
        crs_src = pyproj.CRS.from_wkt(_WKT2)
        transformer = pyproj.Transformer.from_crs(crs_src, "EPSG:4326", always_xy=True)
        corners_x = [x_min, x_min, x_max, x_max]
        corners_y = [y_min, y_max, y_min, y_max]
        lons, lats = transformer.transform(corners_x, corners_y)

        assert west <= min(lons)
        assert east >= max(lons)
        assert south <= min(lats)
        assert north >= max(lats)

    def test_negative_and_overshooting_indices_do_not_raise(self) -> None:
        """Phase-aligned edge windows can have row_start/col_start < 0 or
        row_end/col_end beyond the tile shape (phantom edge pixels) —
        must not raise or produce a degenerate bbox."""
        bbox = _window_bbox_4326((-4, 4, -4, 4), _TRANSFORM, _WKT2)
        west, south, east, north = bbox
        assert west < east
        assert south < north


class TestCropToNativeBounds:
    def test_crops_to_exact_pixel_count(self) -> None:
        # Build a small synthetic native-CRS dataset (x ascending, y descending).
        resolution = 20.0
        x = 3_600_000.0 + (np.arange(50) + 0.5) * resolution
        y = 1_200_000.0 - (np.arange(50) + 0.5) * resolution
        data = np.arange(50 * 50, dtype="uint8").reshape(1, 50, 50)
        ds = xr.Dataset(
            {"band": (("time", "y", "x"), data)},
            coords={"time": [0], "y": y, "x": x},
        )

        window = (10, 15, 5, 12)  # 5 rows x 7 cols
        bounds = _window_native_bounds(window, (resolution, 0.0, 3_600_000.0, 0.0, -resolution, 1_200_000.0))
        cropped = _crop_to_native_bounds(ds, bounds, resolution)

        assert cropped.sizes["y"] == 5
        assert cropped.sizes["x"] == 7

    def test_crop_matches_direct_slice_of_a_larger_load(self) -> None:
        """The crop of a small window from a larger dataset must exactly
        match a direct index-based slice of the same region — this is the
        property that was verified against real EODC data during the Phase
        W.2 investigation (load+crop mechanism itself proven byte-exact)."""
        resolution = 20.0
        n = 100
        x = 3_600_000.0 + (np.arange(n) + 0.5) * resolution
        y = 1_200_000.0 - (np.arange(n) + 0.5) * resolution
        rng = np.random.default_rng(0)
        data = rng.integers(0, 255, size=(1, n, n), dtype="uint8")
        ds = xr.Dataset(
            {"band": (("time", "y", "x"), data)},
            coords={"time": [0], "y": y, "x": x},
        )

        window = (20, 40, 30, 55)
        row_start, row_end, col_start, col_end = window
        expected = data[:, row_start:row_end, col_start:col_end]

        bounds = _window_native_bounds(window, (resolution, 0.0, 3_600_000.0, 0.0, -resolution, 1_200_000.0))
        cropped = _crop_to_native_bounds(ds, bounds, resolution)
        np.testing.assert_array_equal(cropped["band"].values, expected)
