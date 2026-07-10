"""Tests for the stac-geoparquet export (scale path). Skipped if the dep is absent."""

import importlib.util
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from atlantis.archive import grid
from atlantis.archive.writer import ArchiveWriter
from atlantis.stac import build_datacube_catalog

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("stac_geoparquet") is None,
    reason="stac-geoparquet not installed (atlantis[stac])",
)


def _aligned(value: float, row0: int, col0: int, h: int, w: int):
    import xarray as xr

    y = grid.global_y_coords()[row0 : row0 + h]
    x = grid.global_x_coords()[col0 : col0 + w]
    data = np.full((h, w), value, dtype="float32")
    return xr.Dataset(
        {"flood_fraction": xr.DataArray(data, dims=["y", "x"], coords={"y": y, "x": x})},
        attrs={"crs": "EPSG:4326"},
    )


@pytest.fixture(scope="module")
def items(tmp_path_factory):
    root = tmp_path_factory.mktemp("cube_gp")
    writer = ArchiveWriter(root)
    writer.write(_aligned(0.5, 4000, 10000, 50, 60), "viirs", time=date(2020, 1, 1), ensure_masks=True)
    writer.write(_aligned(0.7, 4010, 10010, 40, 40), "viirs", time=date(2020, 1, 3), ensure_masks=True)
    cat = build_datacube_catalog(str(root))
    return list(cat.get_items(recursive=True))


def test_export_writes_parquet(items, tmp_path):
    from atlantis.stac.geoparquet import export_items_to_geoparquet

    dest = tmp_path / "items.parquet"
    out = export_items_to_geoparquet(items, str(dest))
    assert Path(out).exists()

    import geopandas as gpd

    gdf = gpd.read_parquet(dest)
    assert len(gdf) == 2


def test_export_empty_raises(tmp_path):
    from atlantis.stac.geoparquet import export_items_to_geoparquet

    with pytest.raises(ValueError):
        export_items_to_geoparquet([], str(tmp_path / "x.parquet"))


def test_search_by_bbox(items, tmp_path):
    from atlantis.stac.geoparquet import export_items_to_geoparquet, search_geoparquet

    dest = tmp_path / "items.parquet"
    export_items_to_geoparquet(items, str(dest))

    # bbox around the first item's populated window only.
    res = grid.GLOBAL_RESOLUTION
    xv = grid.global_x_coords()
    yv = grid.global_y_coords()
    bbox = (
        float(xv[10000]) - res / 2,
        float(yv[4049]) - res / 2,
        float(xv[10059]) + res / 2,
        float(yv[4000]) + res / 2,
    )
    hit = search_geoparquet(str(dest), bbox=bbox)
    assert len(hit) >= 1
