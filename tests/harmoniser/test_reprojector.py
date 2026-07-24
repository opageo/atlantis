"""Tests for the Reprojector class."""

from __future__ import annotations

from unittest.mock import PropertyMock

import numpy as np
import pytest
from rasterio.transform import from_bounds

from atlantis.harmoniser.reprojector import Reprojector, _resolve_resampling


def _make_test_dataset(
    width: int = 100,
    height: int = 100,
    res: float = 0.004,
    west: float = 20.0,
    north: float = 35.4,
    dtype: str = "uint8",
):
    """Create a synthetic VIIRS-like xarray Dataset."""
    import rioxarray  # noqa: F401
    import xarray as xr

    east = west + width * res
    south = north - height * res

    data = np.random.default_rng(42).integers(0, 2, size=(height, width)).astype(dtype)
    transform = from_bounds(west, south, east, north, width, height)

    ds = xr.Dataset(
        {"flood_extent": xr.DataArray(data, dims=["y", "x"])},
        coords={
            "x": west + (np.arange(width) + 0.5) * res,
            "y": north - (np.arange(height) + 0.5) * res,
        },
    )
    ds.rio.write_crs("EPSG:4326", inplace=True)
    ds.rio.write_transform(transform, inplace=True)
    return ds


class TestReprojector:
    def test_init_defaults(self):
        """Reprojector should use 1 arcmin resolution by default."""
        r = Reprojector()
        assert r.target_crs == "EPSG:4326"
        assert r.target_resolution == pytest.approx(0.016666666666666666)
        assert r.resampling_method == "average"

    def test_init_custom(self):
        r = Reprojector(
            target_crs="EPSG:3857",
            target_resolution=250.0,
            resampling_method="bilinear",
            variable_resampling={"flood_extent": "average", "quality_mask": "mode"},
        )
        assert r.target_crs == "EPSG:3857"
        assert r.target_resolution == 250.0
        assert r.resampling_method == "bilinear"
        assert r.variable_resampling["flood_extent"] == "average"

    def test_reproject_same_crs(self):
        """Same-CRS reproject should resample to target resolution."""
        ds = _make_test_dataset(width=100, height=100, res=0.004)
        r = Reprojector(target_resolution=0.016666666666666666)

        ds_out = r.reproject(ds)

        assert "flood_extent" in ds_out.data_vars
        # 0.4° scene / 0.01667° ≈ 24 pixels
        assert ds_out["flood_extent"].shape[0] < ds["flood_extent"].shape[0]
        assert ds_out["flood_extent"].shape[1] < ds["flood_extent"].shape[1]
        assert ds_out["flood_extent"].dtype == np.float32  # average resampling
        assert ds_out.attrs["processing"] == "harmonised"

    def test_reproject_flood_extent_average(self):
        """Flood extent with average resampling should yield 0-1 fractions."""
        ds = _make_test_dataset(width=50, height=50, res=0.004)
        np.random.seed(42)
        ds["flood_extent"].values[:] = np.random.choice([0, 1], size=(50, 50), p=[0.5, 0.5])

        r = Reprojector(target_resolution=0.016666666666666666, variable_resampling={"flood_extent": "average"})
        ds_out = r.reproject(ds)

        vals = ds_out["flood_extent"].values
        # Grid-snapping can extend the destination slightly beyond source
        # coverage; those border cells are legitimately nodata (NaN), not a
        # fabricated in-range value, so exclude them from the range check.
        valid = vals[~np.isnan(vals)]
        assert valid.size > 0
        assert valid.min() >= 0.0
        assert valid.max() <= 1.0

    def test_reproject_quality_mask_mode(self):
        """Quality mask with mode resampling should stay binary + nodata."""
        ds = _make_test_dataset(width=50, height=50, res=0.004, dtype="uint8")
        ds["quality_mask"] = ds["flood_extent"].copy()
        ds["quality_mask"].values[:] = np.random.choice([0, 1], size=(50, 50), p=[0.2, 0.8])

        r = Reprojector(target_resolution=0.016666666666666666, variable_resampling={"quality_mask": "mode"})
        ds_out = r.reproject(ds)

        if "quality_mask" in ds_out.data_vars:
            vals = ds_out["quality_mask"].values
            assert set(np.unique(vals)).issubset({0, 1, 255})

    def test_reproject_empty_dataset(self):
        """Empty dataset should return a copy."""
        import xarray as xr

        ds = xr.Dataset()
        r = Reprojector()
        ds_out = r.reproject(ds)
        assert len(ds_out.data_vars) == 0

    def test_reproject_different_target_resolution(self):
        """Verify target resolution scaling affects output shape."""
        ds = _make_test_dataset(width=100, height=100, res=0.004)

        # Coarser resolution → smaller output
        r_coarse = Reprojector(target_resolution=0.05)
        ds_coarse = r_coarse.reproject(ds)

        r_fine = Reprojector(target_resolution=0.01)
        ds_fine = r_fine.reproject(ds)

        assert ds_coarse["flood_extent"].size < ds_fine["flood_extent"].size

    def test_reproject_average_nan_nodata_skips_invalid_subpixels(self):
        """Regression: NaN-sentinel source nodata must be excluded from ``average``.

        ``water_fraction``/``flood_fraction`` declare ``nodata=None`` and use an
        in-array ``NaN`` sentinel for excluded pixels (no ``_FillValue``/``nodata``
        metadata). A destination pixel whose footprint mixes valid and NaN
        sub-pixels must be averaged over the valid sub-pixels only, not
        collapse to NaN just because one contributing sub-pixel is NaN.
        """
        import rioxarray  # noqa: F401
        import xarray as xr

        width, height, res = 4, 4, 1.0
        west, north = 0.0, 4.0
        east = west + width * res
        south = north - height * res
        transform = from_bounds(west, south, east, north, width, height)

        # Checkerboard of valid (1.0) and excluded (NaN) sub-pixels: each 2x2
        # destination block mixes two valid and two NaN sub-pixels.
        data = np.array(
            [
                [1.0, np.nan, 1.0, np.nan],
                [np.nan, 1.0, np.nan, 1.0],
                [1.0, np.nan, 1.0, np.nan],
                [np.nan, 1.0, np.nan, 1.0],
            ],
            dtype=np.float32,
        )
        # No _FillValue/nodata attrs set, matching water_fraction/flood_fraction's
        # `nodata=None` spec (NaN is an in-array sentinel, not declared metadata).
        da = xr.DataArray(data, dims=["y", "x"])
        ds = xr.Dataset(
            {"water_fraction": da},
            coords={
                "x": west + (np.arange(width) + 0.5) * res,
                "y": north - (np.arange(height) + 0.5) * res,
            },
        )
        ds.rio.write_crs("EPSG:4326", inplace=True)
        ds.rio.write_transform(transform, inplace=True)

        r = Reprojector(
            target_resolution=res * 2,
            variable_resampling={"water_fraction": "average"},
            snap_to_global_grid=False,
        )
        ds_out = r.reproject(ds)

        vals = ds_out["water_fraction"].values
        assert not np.any(np.isnan(vals)), f"NaN sub-pixels poisoned the average: {vals}"
        np.testing.assert_allclose(vals, np.ones((2, 2), dtype=np.float32), atol=1e-5)

    def test_integer_nearest_trims_uncovered_snapped_border(self):
        """Uncovered destination cells must be trimmed away, not left as nodata.

        Regression guard for native-code harmonisation: snapped AOI bounds can
        extend slightly beyond source coverage. Those border pixels must not
        be cast to valid code ``0`` (original guard) -- and, since
        `Reprojector._trim_uncovered_margin` was added, must no longer remain
        in the output as a nodata sentinel either: the whole partially/
        uncovered strip is dropped so the returned dataset is fully valid.
        """
        import rioxarray  # noqa: F401
        import xarray as xr

        width, height, res = 4, 4, 0.02
        west, north = -0.861, 11.731
        east = west + width * res
        south = north - height * res
        transform = from_bounds(west, south, east, north, width, height)

        data = np.ones((height, width), dtype=np.uint8)
        da = xr.DataArray(
            data,
            dims=["y", "x"],
            attrs={"_FillValue": 255},
        )
        da.rio.write_nodata(255, inplace=True)
        ds = xr.Dataset(
            {"flood_extent": da},
            coords={
                "x": west + (np.arange(width) + 0.5) * res,
                "y": north - (np.arange(height) + 0.5) * res,
            },
        )
        ds.rio.write_crs("EPSG:4326", inplace=True)
        ds.rio.write_transform(transform, inplace=True)

        r = Reprojector(
            target_resolution=1.0 / 60.0,
            variable_resampling={"flood_extent": "nearest"},
            snap_to_global_grid=True,
        )
        ds_out = r.reproject(ds)

        vals = ds_out["flood_extent"].values
        unique = set(np.unique(vals).tolist())
        assert unique == {1}, f"Partially/uncovered border must be trimmed away entirely, found: {unique}"

        # Confirm the grid actually shrank (this source is off-grid on all
        # four sides), not merely that it coincidentally has no nodata.
        snapped_west, snapped_south, snapped_east, snapped_north = r._snap_bounds_to_global_grid(
            west, south, east, north
        )
        untrimmed_width = round((snapped_east - snapped_west) / r.target_resolution)
        untrimmed_height = round((snapped_north - snapped_south) / r.target_resolution)
        assert ds_out["flood_extent"].shape[1] == untrimmed_width - 2
        assert ds_out["flood_extent"].shape[0] == untrimmed_height - 2

    def test_average_partial_coverage_on_snapped_border(self):
        """Diagnostic: border cells with PARTIAL (not zero) source coverage.

        Complements ``test_integer_nearest_preserves_nodata_on_snapped_border``,
        which only exercises *zero*-coverage border cells under ``nearest``.
        Here the outward-snapped margin overlaps the source by a fraction of a
        destination pixel (neither ~0% nor ~100%) under ``average`` resampling
        -- the scenario behind issue #109's edge artefact. The source is a
        uniform constant so any leak of real data into these cells is
        unambiguous: the result must be either NaN or exactly the constant
        (correctly averaged over whatever fraction of real sub-pixels is
        actually present) -- never some other spurious value.

        This test is intentionally diagnostic (see module docstring / PR
        notes): its purpose is to establish *what the border cells actually
        contain* so that the border-trimming fix knows whether "trim
        pure-fill rows/cols" is sufficient, or whether partially-covered
        non-fill border cells also need to be trimmed.
        """
        import rioxarray  # noqa: F401
        import xarray as xr

        # Off-grid origin (same style as the West-Africa-like snap tests)
        # with native resolution fine enough that each destination pixel
        # spans ~3-4 source sub-pixels, so border coverage is meaningfully
        # fractional rather than all-or-nothing.
        width, height, res = 40, 40, 0.005
        west, north = -0.861, 11.731
        east = west + width * res
        south = north - height * res
        transform = from_bounds(west, south, east, north, width, height)

        marker = 7.0
        data = np.full((height, width), marker, dtype=np.float32)
        da = xr.DataArray(data, dims=["y", "x"])
        ds = xr.Dataset(
            {"water_fraction": da},
            coords={
                "x": west + (np.arange(width) + 0.5) * res,
                "y": north - (np.arange(height) + 0.5) * res,
            },
        )
        ds.rio.write_crs("EPSG:4326", inplace=True)
        ds.rio.write_transform(transform, inplace=True)

        r = Reprojector(
            target_resolution=1.0 / 60.0,
            variable_resampling={"water_fraction": "average"},
            snap_to_global_grid=True,
        )
        ds_out = r.reproject(ds)

        vals = ds_out["water_fraction"].values
        border = np.concatenate([vals[0, :], vals[-1, :], vals[:, 0], vals[:, -1]])
        spurious = border[~np.isnan(border) & ~np.isclose(border, marker)]
        assert spurious.size == 0, (
            f"Border cells must be NaN or exactly the source constant ({marker}); "
            f"found spurious values instead: {spurious}"
        )

        n_nan = int(np.isnan(border).sum())
        n_marker = int(np.isclose(border, marker).sum())
        print(
            f"\n[diagnostic] border cells: {n_nan} NaN, {n_marker} == marker "
            f"(out of {border.size}); vals[0,:5]={vals[0, :5]}; vals[:5,0]={vals[:5, 0]}"
        )


class TestResolveResampling:
    def test_valid_methods(self):
        from rasterio.enums import Resampling

        assert _resolve_resampling("average") == Resampling.average
        assert _resolve_resampling("bilinear") == Resampling.bilinear
        assert _resolve_resampling("nearest") == Resampling.nearest
        assert _resolve_resampling("mode") == Resampling.mode

    def test_case_insensitive(self):
        from rasterio.enums import Resampling

        assert _resolve_resampling("NEAREST") == Resampling.nearest
        assert _resolve_resampling("  Average  ") == Resampling.average

    def test_invalid_method(self):
        with pytest.raises(ValueError, match="Unsupported resampling method"):
            _resolve_resampling("invalid_method")


class TestSnapToGlobalGrid:
    """Verify snapping of AOI bounds onto the canonical 1-arcmin global grid.

    The reference grid (e.g. ECMWF ``Globe_flood_area_*.grb``) has dims
    ``lat=10800, lon=21600`` with pixel centres at ``\u00b1(k + 0.5)/60``
    degrees, i.e. anchored at western edge ``-180`` and northern edge ``+90``.
    """

    RES = 1.0 / 60.0
    LON0 = -180.0
    LAT0 = 90.0

    def test_snap_offgrid_window_extends_outward(self):
        """A bbox not on the grid is extended outward to the nearest cell edges."""
        r = Reprojector(target_resolution=self.RES)
        # West Africa-like bbox, intentionally off-grid by sub-arcmin.
        west, south, east, north = r._snap_bounds_to_global_grid(-0.86, 8.26, 1.99, 11.73)

        # Snapped values must lie exactly on multiples of RES from origins.
        for v, origin in [
            (west - self.LON0, "west"),
            (east - self.LON0, "east"),
        ]:
            assert v / self.RES == pytest.approx(round(v / self.RES), abs=1e-9), origin
        for v, origin in [
            (self.LAT0 - north, "north"),
            (self.LAT0 - south, "south"),
        ]:
            assert v / self.RES == pytest.approx(round(v / self.RES), abs=1e-9), origin

        # Outward expansion only.
        assert west <= -0.86
        assert south <= 8.26
        assert east >= 1.99
        assert north >= 11.73

    def test_snapped_pixel_centres_match_canonical(self):
        """After snapping, pixel centres equal -180+(k+0.5)/60 and 90-(j+0.5)/60."""
        ds = _make_test_dataset(width=10, height=10, res=0.004, west=-0.86, north=11.73)
        r = Reprojector(target_resolution=self.RES, snap_to_global_grid=True)
        ds_out = r.reproject(ds)

        x = ds_out["x"].values
        y = ds_out["y"].values

        # Each x must be -180 + (k+0.5)/60 for some integer k.
        kx = (x - self.LON0) / self.RES - 0.5
        ky = (self.LAT0 - y) / self.RES - 0.5
        np.testing.assert_allclose(kx, np.round(kx), atol=1e-9)
        np.testing.assert_allclose(ky, np.round(ky), atol=1e-9)

        # Pixel size in the output transform equals exactly 1/60.
        assert ds_out.attrs["target_resolution"] == pytest.approx(self.RES, abs=1e-12)

    def test_snap_disabled_preserves_aoi_bounds(self):
        """With snapping off, output bounds match the input AOI."""
        ds = _make_test_dataset(width=10, height=10, res=0.004, west=-0.86, north=11.73)
        r = Reprojector(target_resolution=self.RES, snap_to_global_grid=False)
        ds_out = r.reproject(ds)

        # First pixel centre = west + 0.5 * pixel_size; should NOT be on the canonical grid.
        first_x = float(ds_out["x"].values[0])
        kx = (first_x - self.LON0) / self.RES - 0.5
        assert abs(kx - round(kx)) > 1e-3  # genuinely off the grid

    def test_snap_skipped_for_non_geographic_crs(self):
        """Snapping is a lat/lon construct; it must not engage for projected CRS."""
        r = Reprojector(
            target_crs="EPSG:3857",
            target_resolution=250.0,
            snap_to_global_grid=True,
        )
        # The helper itself is only invoked for EPSG:4326; calling reproject
        # with a 4326 dataset against a 3857 target should not touch the
        # global-grid path, so this just checks the gate in reproject() does
        # not raise. (We exercise the public path.)
        ds = _make_test_dataset(width=10, height=10, res=0.004)
        ds_out = r.reproject(ds)
        assert ds_out["flood_extent"].size > 0

    def test_snap_clips_to_global_extent(self):
        """Snapped bounds never exceed the global lat/lon extent."""
        r = Reprojector(target_resolution=self.RES)
        west, south, east, north = r._snap_bounds_to_global_grid(-179.99, -89.99, 179.99, 89.99)
        assert west >= -180.0
        assert east <= 180.0
        assert south >= -90.0
        assert north <= 90.0


class TestReprojectorIdempotentFastPath:
    """Reprojector must be a no-op when the dataset is already on the canonical grid.

    GFM data arrives pre-projected to the 1-arcmin canonical grid.  A second
    reproject() call (from the harmoniser) must short-circuit rather than
    re-warp, preserving pixel values exactly.
    """

    RES = 1.0 / 60.0  # 1 arcmin

    def _make_canonical_dataset(self, west: float = -97.3, north: float = 29.8):
        """Build a Dataset already on the 1-arcmin snapped grid."""
        import rioxarray  # noqa: F401
        import xarray as xr

        # Snap bounds so pixel centres land on the canonical grid.
        r = Reprojector(target_resolution=self.RES)
        w, s, e, n = r._snap_bounds_to_global_grid(west, north - 1.0, west + 1.0, north)
        width = max(1, int(round((e - w) / self.RES)))
        height = max(1, int(round((n - s) / self.RES)))
        transform = from_bounds(w, s, e, n, width, height)

        data = np.random.default_rng(7).random((height, width)).astype(np.float32)
        x_coords = w + (np.arange(width) + 0.5) * self.RES
        y_coords = n - (np.arange(height) + 0.5) * self.RES
        ds = xr.Dataset(
            {"flood_fraction": xr.DataArray(data, dims=["y", "x"], coords={"x": x_coords, "y": y_coords})},
        )
        ds.rio.write_crs("EPSG:4326", inplace=True)
        ds.rio.write_transform(transform, inplace=True)
        return ds

    def test_fast_path_triggered_for_canonical_grid(self):
        """reproject() must take the fast-path and return identical values."""
        ds = self._make_canonical_dataset()
        r = Reprojector(target_resolution=self.RES, snap_to_global_grid=True)
        ds_out = r.reproject(ds)

        np.testing.assert_array_equal(
            ds_out["flood_fraction"].values,
            ds["flood_fraction"].values,
        )
        assert ds_out.attrs["processing"] == "harmonised"

    def test_fast_path_adds_provenance_attrs(self):
        """Even with the fast-path, processing attrs must be set."""
        ds = self._make_canonical_dataset()
        r = Reprojector(target_resolution=self.RES)
        ds_out = r.reproject(ds)
        assert "processing" in ds_out.attrs
        assert ds_out.attrs["target_resolution"] == pytest.approx(self.RES, abs=1e-12)

    def test_fast_path_not_triggered_for_different_resolution(self):
        """A dataset at 2-arcmin resolution must NOT take the fast-path."""
        ds = self._make_canonical_dataset()
        r = Reprojector(target_resolution=self.RES * 2, snap_to_global_grid=True)
        ds_out = r.reproject(ds)
        # Output shape should differ (coarser grid).
        assert ds_out["flood_fraction"].shape != ds["flood_fraction"].shape


class TestReprojectorHelpers:
    """Tests for reprojector utility functions."""

    def test_rio_available_false_on_exception(self) -> None:
        """_rio_available returns False when .rio.crs raises."""
        from unittest.mock import MagicMock

        from atlantis.harmoniser.reprojector import _rio_available

        ds = MagicMock()
        type(ds.rio).crs = PropertyMock(side_effect=RuntimeError("no rio"))
        assert _rio_available(ds) is False

    def test_get_dataset_bounds_no_rio_falls_back_to_xy(self) -> None:
        """Falls back to x/y coords when rio is unavailable."""
        from unittest.mock import MagicMock

        from atlantis.harmoniser.reprojector import _get_dataset_bounds

        ds = MagicMock()
        ds.coords = {"x": MagicMock(), "y": MagicMock()}
        ds.coords["x"].values = np.array([10.0, 20.0])
        ds.coords["y"].values = np.array([30.0, 40.0])
        type(ds.rio).crs = PropertyMock(side_effect=RuntimeError)

        result = _get_dataset_bounds(ds)
        assert result == (10.0, 30.0, 20.0, 40.0)

    def test_get_dataset_bounds_lon_lat_fallback(self) -> None:
        """Falls back to lon/lat coords when x/y aren't present."""
        from unittest.mock import MagicMock

        from atlantis.harmoniser.reprojector import _get_dataset_bounds

        ds = MagicMock()
        ds.coords = {"lon": MagicMock(), "lat": MagicMock()}
        ds.coords["lon"].values = np.array([-10.0, 10.0])
        ds.coords["lat"].values = np.array([-20.0, 20.0])
        type(ds.rio).crs = PropertyMock(side_effect=RuntimeError)

        result = _get_dataset_bounds(ds)
        assert result == (-10.0, -20.0, 10.0, 20.0)

    def test_get_dataset_bounds_rio_exception_falls_back(self) -> None:
        """When rio.bounds() raises, falls back to coordinates."""
        from unittest.mock import MagicMock

        from atlantis.harmoniser.reprojector import _get_dataset_bounds

        ds = MagicMock()
        ds.coords = {"x": MagicMock(), "y": MagicMock()}
        ds.coords["x"].values = np.array([1.0, 2.0])
        ds.coords["y"].values = np.array([3.0, 4.0])
        type(ds.rio).crs = PropertyMock(return_value="EPSG:4326")
        ds.rio.bounds.side_effect = RuntimeError

        result = _get_dataset_bounds(ds)
        assert result == (1.0, 3.0, 2.0, 4.0)
