"""Tests for the Reprojector class."""

from __future__ import annotations

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
        assert vals.min() >= 0.0
        assert vals.max() <= 1.0

    def test_reproject_quality_mask_mode(self):
        """Quality mask with mode resampling should stay binary."""
        ds = _make_test_dataset(width=50, height=50, res=0.004, dtype="uint8")
        ds["quality_mask"] = ds["flood_extent"].copy()
        ds["quality_mask"].values[:] = np.random.choice([0, 1], size=(50, 50), p=[0.2, 0.8])

        r = Reprojector(target_resolution=0.016666666666666666, variable_resampling={"quality_mask": "mode"})
        ds_out = r.reproject(ds)

        if "quality_mask" in ds_out.data_vars:
            vals = ds_out["quality_mask"].values
            assert set(np.unique(vals)).issubset({0, 1})

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
