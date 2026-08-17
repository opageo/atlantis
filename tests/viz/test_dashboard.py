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
        {"water_fraction": (["time", "y", "x"], data)},
        coords={"time": t, "y": y, "x": x},
    )


def test_build_dashboard_with_time_slider_returns_dynamicmap():
    import holoviews as hv

    from atlantis.viz import build_cube_dashboard

    obj = build_cube_dashboard(ds=_ds(2), source="viirs", var="water_fraction", rasterize=False, basemap=False)
    # groupby="time" produces a DynamicMap / HoloMap with a time widget.
    assert isinstance(obj, (hv.DynamicMap, hv.HoloMap))


def test_build_dashboard_single_time_returns_element():
    from atlantis.viz import build_cube_dashboard

    obj = build_cube_dashboard(ds=_ds(1).isel(time=0), source="viirs", rasterize=False, basemap=False)
    assert obj is not None
    assert type(obj).__name__ in {"Image", "QuadMesh", "DynamicMap"}


def _empty_time_ds():
    """In-memory datacube-style dataset whose ``time`` axis is empty (size 0)."""
    import xarray as xr

    y = grid.global_y_coords()[4000:4050]
    x = grid.global_x_coords()[10000:10060]
    return xr.Dataset(
        {"water_fraction": (["time", "y", "x"], np.empty((0, y.size, x.size)))},
        coords={"time": np.array([], dtype="datetime64[ns]"), "y": y, "x": x},
    )


def test_build_dashboard_empty_time_is_unbounded_but_constructible():
    from atlantis.viz import build_cube_dashboard

    # An empty time axis (e.g. --start/--end matching no dates) must not
    # crash at build time; the dim stays unbounded and is reported by serve.
    obj = build_cube_dashboard(
        ds=_empty_time_ds(), source="viirs", var="water_fraction", rasterize=False, basemap=False
    )
    assert getattr(obj, "unbounded", [])  # ['time'] — empty axis cannot be bounded


def test_serve_dashboard_empty_time_raises_actionable_error():
    from atlantis.viz import serve_dashboard

    with pytest.raises(ValueError, match="time axis has 0 step"):
        serve_dashboard(
            "viirs",
            ds=_empty_time_ds(),
            var="water_fraction",
            rasterize=False,
            basemap=False,
            host="localhost",
            port=5007,
            show=False,
        )


def test_missing_variable_raises():
    from atlantis.viz import build_cube_dashboard

    with pytest.raises(KeyError):
        build_cube_dashboard(ds=_ds(2), var="not_a_var", rasterize=False)


_no_geoviews = pytest.mark.skipif(
    importlib.util.find_spec("geoviews") is None,
    reason="geoviews not installed (atlantis[viz])",
)


@_no_geoviews
def test_basemap_overlays_coastline_and_borders():
    from atlantis.viz import build_cube_dashboard

    # Single time step → an Overlay whose top layers are the vector features.
    obj = build_cube_dashboard(ds=_ds(1).isel(time=0), source="viirs", rasterize=False, basemap=True)
    layers = [k[0] for k in obj.keys()]
    assert "Coastline" in layers
    assert "Borders" in layers


@_no_geoviews
def test_tiles_adds_web_basemap_under_data():
    from atlantis.viz import build_cube_dashboard

    obj = build_cube_dashboard(ds=_ds(1).isel(time=0), source="viirs", rasterize=False, tiles=True)
    layers = [k[0] for k in obj.keys()]
    # WMTS tiles sit under the data; without basemap there are no vector features.
    assert "WMTS" in layers
    assert "Coastline" not in layers
