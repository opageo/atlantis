"""Tests for the STAC catalog built over the Zarr datacube."""

from datetime import date
from pathlib import Path

import numpy as np
import pytest

from atlantis.archive import grid
from atlantis.archive.writer import ArchiveWriter
from atlantis.config import StacConfig
from atlantis.stac import BuildProgress, build_datacube_catalog, write_catalog
from atlantis.stac.datacube_catalog import XARRAY_ASSETS_EXT

_ROW0, _COL0, _H, _W = 4000, 10000, 50, 60


def _aligned(value: float, row0: int, col0: int, h: int, w: int):
    """Harmonised-style float dataset aligned to the global 1-arcmin grid."""
    import xarray as xr

    y = grid.global_y_coords()[row0 : row0 + h]
    x = grid.global_x_coords()[col0 : col0 + w]
    data = np.full((h, w), value, dtype="float32")
    return xr.Dataset(
        {"flood_fraction": xr.DataArray(data, dims=["y", "x"], coords={"y": y, "x": x})},
        attrs={"crs": "EPSG:4326"},
    )


def _window_bbox(row0: int, col0: int, h: int, w: int):
    """Expected populated bbox (pixel-centre ± half-resolution)."""
    res = grid.GLOBAL_RESOLUTION
    yv = grid.global_y_coords()
    xv = grid.global_x_coords()
    north = float(yv[row0]) + res / 2.0
    south = float(yv[row0 + h - 1]) - res / 2.0
    west = float(xv[col0]) - res / 2.0
    east = float(xv[col0 + w - 1]) + res / 2.0
    return (west, south, east, north)


@pytest.fixture(scope="module")
def cube_dir(tmp_path_factory) -> Path:
    """A two-date single-source (viirs) datacube, built once for the module."""
    root = tmp_path_factory.mktemp("cube")
    writer = ArchiveWriter(root)
    writer.write(_aligned(0.5, _ROW0, _COL0, _H, _W), "viirs", time=date(2020, 1, 1), ensure_masks=True)
    writer.write(_aligned(0.7, _ROW0 + 10, _COL0 + 10, 40, 40), "viirs", time=date(2020, 1, 3), ensure_masks=True)
    return root


@pytest.fixture(scope="module")
def catalog(cube_dir):
    """The default catalog built once and shared across read-only assertions."""
    return build_datacube_catalog(str(cube_dir))


class TestBuildDatacubeCatalog:
    def test_collection_per_source(self, catalog):
        assert [c.id for c in catalog.get_children()] == ["atlantis-datacube-viirs"]

    def test_item_per_populated_date(self, catalog):
        col = next(catalog.get_children())
        items = sorted(col.get_items(), key=lambda i: i.id)
        assert [i.id for i in items] == ["viirs-2020-01-01", "viirs-2020-01-03"]
        assert items[0].datetime.date() == date(2020, 1, 1)

    def test_zarr_asset_and_xarray_extension(self, catalog):
        col = next(catalog.get_children())
        asset = col.assets["zarr"]
        assert asset.media_type == "application/vnd+zarr"
        assert asset.roles == ["data"]
        open_kwargs = asset.extra_fields["xarray:open_kwargs"]
        assert open_kwargs["engine"] == "zarr"
        assert open_kwargs["group"] == "viirs"
        assert XARRAY_ASSETS_EXT in col.stac_extensions

    def test_item_carries_datacube_dimensions_and_variables(self, catalog):
        item = next(next(catalog.get_children()).get_items())
        assert set(item.properties["cube:dimensions"]) == {"x", "y", "time"}
        assert "flood_fraction" in item.properties["cube:variables"]

    def test_item_bbox_is_populated_window(self, catalog):
        col = next(catalog.get_children())
        item = {i.id: i for i in col.get_items()}["viirs-2020-01-01"]
        assert item.bbox == pytest.approx(list(_window_bbox(_ROW0, _COL0, _H, _W)), abs=1e-6)

    def test_per_date_bboxes_differ(self, catalog):
        col = next(catalog.get_children())
        bboxes = {tuple(round(v, 4) for v in i.bbox) for i in col.get_items()}
        assert len(bboxes) == 2  # each date has its own populated extent

    def test_compute_bbox_false_uses_single_extent(self, cube_dir):
        cfg = StacConfig(compute_item_bbox=False)
        cat = build_datacube_catalog(str(cube_dir), stac_config=cfg)
        col = next(cat.get_children())
        bboxes = {tuple(i.bbox) for i in col.get_items()}
        assert len(bboxes) == 1

    def test_sources_filter_skips_absent(self, cube_dir):
        cat = build_datacube_catalog(str(cube_dir), sources=["modis"])
        assert list(cat.get_children()) == []

    def test_empty_archive_yields_no_collections(self, tmp_path):
        cat = build_datacube_catalog(str(tmp_path))
        assert list(cat.get_children()) == []

    def test_progress_callbacks_are_invoked(self, cube_dir):
        events: list[tuple] = []
        progress = BuildProgress(
            on_sources=lambda s: events.append(("sources", tuple(s))),
            on_source_start=lambda s: events.append(("start", s)),
            on_source_total=lambda s, n: events.append(("total", s, n)),
            on_item=lambda s: events.append(("item", s)),
            on_source_done=lambda s, n: events.append(("done", s, n)),
        )
        build_datacube_catalog(str(cube_dir), progress=progress)

        assert ("sources", ("viirs",)) in events
        assert ("start", "viirs") in events
        assert ("total", "viirs", 2) in events
        assert events.count(("item", "viirs")) == 2
        assert ("done", "viirs", 2) in events


class TestWriteCatalog:
    def test_write_and_reopen_roundtrip(self, catalog, tmp_path):
        from pystac import Catalog
        from pystac.errors import STACValidationError

        dest = write_catalog(catalog, str(tmp_path / "stac"))

        reopened = Catalog.from_file(str(Path(dest) / "catalog.json"))
        assert sum(1 for _ in reopened.get_items(recursive=True)) == 2

        try:
            validated = reopened.validate_all()
        except STACValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - offline schema fetch → skip
            pytest.skip(f"schema validation unavailable: {exc}")
        assert validated >= 1
