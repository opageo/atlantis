"""Tests for the Normaliser class."""

from __future__ import annotations

import numpy as np
import pytest

from atlantis.harmoniser.normaliser import Normaliser, NormaliserConfig


def _make_flood_dataset():
    """Create a minimal xarray Dataset with flood_extent and quality_mask."""
    import rioxarray  # noqa: F401
    import xarray as xr

    ds = xr.Dataset(
        {
            "flood_extent": xr.DataArray(
                np.array([[0, 0, 0, 0, 0], [0, 0, 1, 1, 0], [0, 0, 0, 0, 0]], dtype=np.float32),
                dims=["y", "x"],
            ),
            "quality_mask": xr.DataArray(
                np.ones((3, 5), dtype=np.uint8),
                dims=["y", "x"],
            ),
            "permanent_water": xr.DataArray(
                np.zeros((3, 5), dtype=np.uint8),
                dims=["y", "x"],
            ),
        },
        attrs={"cloud_fraction": 0.05},
    )
    return ds


class TestNormaliser:
    def test_init_defaults(self):
        n = Normaliser()
        assert n.config.normalise_range == (0.0, 1.0)
        assert n.config.fill_value == -9999.0
        assert n.config.clip is True

    def test_init_custom_config(self):
        cfg = NormaliserConfig(normalise_range=(0.0, 255.0), fill_value=0.0, clip=False)
        n = Normaliser(config=cfg)
        assert n.config.normalise_range == (0.0, 255.0)

    def test_normalise_flood_extent(self):
        """Flood extent should be normalised to 0-1."""
        ds = _make_flood_dataset()
        n = Normaliser()
        ds_out = n.normalise(ds, variable="flood_extent")
        vals = ds_out["flood_extent"].values
        assert vals.min() >= 0.0
        assert vals.max() <= 1.0
        assert np.isnan(vals).sum() == 0  # no NaN in this case

    def test_normalise_skip_mask(self):
        """quality_mask should be skipped (kept as-is)."""
        ds = _make_flood_dataset()
        n = Normaliser()
        ds_out = n.normalise(ds, variable="quality_mask")
        # Should not be modified
        assert ds_out["quality_mask"].dtype == np.uint8

    def test_normalise_nan_handling(self):
        """Fill values should be replaced with NaN."""
        import xarray as xr

        ds = xr.Dataset(
            {"flood_extent": xr.DataArray(np.array([[1.0, -9999.0, 0.5]], dtype=np.float32), dims=["y", "x"])},
        )
        n = Normaliser()
        ds_out = n.normalise(ds, variable="flood_extent")
        # -9999.0 should become NaN
        assert np.isnan(ds_out["flood_extent"].values[0, 1])

    def test_normalise_missing_variable(self):
        ds = _make_flood_dataset()
        n = Normaliser()
        with pytest.raises(KeyError, match="nonexistent"):
            n.normalise(ds, variable="nonexistent")

    def test_generate_quality_from_quality_variable(self):
        """When quality_mask exists, it should be used directly."""
        ds = _make_flood_dataset()
        n = Normaliser()
        qm = n.generate_quality_mask(ds)
        assert qm.dtype == np.uint8
        assert qm.shape == (3, 5)

    def test_generate_quality_from_nan(self):
        """When no quality_mask exists, NaN-derived mask should be generated."""
        import xarray as xr

        ds = xr.Dataset(
            {"flood_extent": xr.DataArray(np.array([[0.0, np.nan, 1.0]], dtype=np.float32), dims=["y", "x"])},
        )
        n = Normaliser()
        qm = n.generate_quality_mask(ds, variable="flood_extent")
        assert qm[0, 1] == 1  # NaN → nodata flag
        assert qm[0, 0] == 0  # valid

    def test_generate_permanent_water_exists(self):
        """When permanent_water variable exists, it should be extracted."""
        ds = _make_flood_dataset()
        n = Normaliser()
        pw = n.generate_permanent_water_mask(ds)
        assert pw.dtype == np.uint8
        assert pw.shape == (3, 5)

    def test_generate_permanent_water_empty(self):
        """When no permanent_water variable exists, return zeros."""
        import xarray as xr

        ds = xr.Dataset(
            {"flood_extent": xr.DataArray(np.zeros((10, 10), dtype=np.float32), dims=["y", "x"])},
        )
        n = Normaliser()
        pw = n.generate_permanent_water_mask(ds)
        assert pw.shape == (10, 10)
        assert pw.sum() == 0
