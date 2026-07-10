"""Integration test: open the datacube through STAC via xpystac. Skipped if absent."""

import importlib.util
from datetime import date

import numpy as np
import pytest

from atlantis.archive import grid
from atlantis.archive.reader import ArchiveReader
from atlantis.archive.writer import ArchiveWriter
from atlantis.stac import build_datacube_catalog, write_catalog

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("xpystac") is None,
    reason="xpystac not installed (atlantis[stac])",
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
def catalog_dir(tmp_path_factory):
    root = tmp_path_factory.mktemp("cube_stac")
    writer = ArchiveWriter(root)
    writer.write(_aligned(0.5, 4000, 10000, 50, 60), "viirs", time=date(2020, 1, 1), ensure_masks=True)
    writer.write(_aligned(0.7, 4010, 10010, 40, 40), "viirs", time=date(2020, 1, 3), ensure_masks=True)
    dest = write_catalog(build_datacube_catalog(str(root)), str(root / "stac"))
    return str(root), dest


def test_from_stac_matches_reader(catalog_dir):
    from atlantis.viz import from_stac

    root, dest = catalog_dir
    ds = from_stac(dest, "viirs")
    assert "flood_fraction" in ds
    assert ds.sizes["time"] == 2

    ref = ArchiveReader(root).read("viirs")
    stac_max = float(ds["flood_fraction"].sel(time="2020-01-01").max().values)
    ref_max = float(ref["flood_fraction"].sel(time="2020-01-01").max().values)
    assert np.isclose(stac_max, ref_max)


def test_from_stac_bbox_subsets(catalog_dir):
    from atlantis.viz import from_stac

    root, dest = catalog_dir
    res = grid.GLOBAL_RESOLUTION
    xv = grid.global_x_coords()
    yv = grid.global_y_coords()
    bbox = (
        float(xv[10000]) - res / 2,
        float(yv[4049]) - res / 2,
        float(xv[10059]) + res / 2,
        float(yv[4000]) + res / 2,
    )
    # from_stac and the direct reader share grid.bounds_to_window, so windows match.
    ds = from_stac(dest, "viirs", bbox=bbox)
    ref = ArchiveReader(root).read("viirs", bbox=bbox)
    assert ds.sizes["y"] == ref.sizes["y"] > 0
    assert ds.sizes["x"] == ref.sizes["x"] > 0
