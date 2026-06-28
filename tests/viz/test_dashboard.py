"""Tests for the HoloViz datacube dashboard. Skipped if hvplot is absent."""

import importlib.util

import numpy as np
import pytest

from atlantis.archive import grid

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("hvplot") is None,
    reason="hvplot not installed (atlantis[viz])",
)


def _ds(times: int = 2):
    """In-memory datacube-style dataset with a time dimension."""
    import xarray as xr

    y = grid.global_y_coords()[4000:4050]
    x = grid.global_x_coords()[10000:10060]
    t = np.array(["2020-01-01", "2020-01-03"][:times], dtype="datetime64[ns]")
    rng = np.random.default_rng(0)
    data = rng.random((times, y.size, x.size), dtype="float32")
    return xr.Dataset(
        {"flood_fraction": (["time", "y", "x"], data)},
        coords={"time": t, "y": y, "x": x},
    )


def test_build_dashboard_with_time_slider_returns_dynamicmap():
    import holoviews as hv

    from atlantis.viz import build_cube_dashboard

    obj = build_cube_dashboard(ds=_ds(2), source="viirs", var="flood_fraction", rasterize=False, basemap=False)
    # groupby="time" produces a DynamicMap / HoloMap with a time widget.
    assert isinstance(obj, (hv.DynamicMap, hv.HoloMap))


def test_build_dashboard_single_time_returns_element():
    from atlantis.viz import build_cube_dashboard

    obj = build_cube_dashboard(ds=_ds(1).isel(time=0), source="viirs", rasterize=False, basemap=False)
    assert obj is not None
    assert type(obj).__name__ in {"Image", "QuadMesh", "DynamicMap"}


def test_missing_variable_raises():
    from atlantis.viz import build_cube_dashboard

    with pytest.raises(KeyError):
        build_cube_dashboard(ds=_ds(2), var="not_a_var", rasterize=False)
