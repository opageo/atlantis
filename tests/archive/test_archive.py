"""Tests for the consolidated Zarr datacube archive."""

from datetime import date
from pathlib import Path

import numpy as np
import pytest

from atlantis.archive import grid
from atlantis.archive.reader import ArchiveReader
from atlantis.archive.writer import ArchiveWriter
from atlantis.config import ArchiveConfig
from atlantis.models.event import FloodEvent

# Default AOI window on the canonical global grid used across tests.
_ROW0, _COL0, _H, _W = 4000, 10000, 50, 60


def aligned_dataset(value: float = 0.5, *, row0: int = _ROW0, col0: int = _COL0, h: int = _H, w: int = _W):
    """Build a harmonised-style float dataset aligned to the global 1-arcmin grid."""
    import xarray as xr

    y = grid.global_y_coords()[row0 : row0 + h]
    x = grid.global_x_coords()[col0 : col0 + w]
    data = np.full((h, w), value, dtype="float32")
    return xr.Dataset(
        {"flood_fraction": xr.DataArray(data, dims=["y", "x"], coords={"y": y, "x": x})},
        attrs={"crs": "EPSG:4326"},
    )


def _zarr_array(store, source_id: str, var: str):
    import zarr

    return zarr.open_group(store, mode="r")[source_id][var]


def window_bbox(row0: int = _ROW0, col0: int = _COL0, h: int = _H, w: int = _W):
    """Geographic bbox (west, south, east, north) of a grid window."""
    res = grid.GLOBAL_RESOLUTION
    west = grid.ORIGIN_LON + col0 * res
    east = grid.ORIGIN_LON + (col0 + w) * res
    north = grid.ORIGIN_LAT - row0 * res
    south = grid.ORIGIN_LAT - (row0 + h) * res
    return (west, south, east, north)


@pytest.fixture()
def event() -> FloodEvent:
    return FloodEvent(
        event_id="test_event",
        bbox=window_bbox(),
        start_date=date(2020, 1, 1),
        end_date=date(2020, 1, 5),
        sources=["viirs"],
    )


@pytest.fixture()
def simple_dataset():
    return aligned_dataset(0.5)


# ── ArchiveWriter (datacube) ──────────────────────────────────────────────────


class TestArchiveWriter:
    def test_write_creates_consolidated_sharded_cube(self, tmp_path, simple_dataset):
        store = ArchiveWriter(tmp_path).write(simple_dataset, "viirs", time=date(2020, 1, 1))
        # A single consolidated store, grouped by source, on the global grid.
        assert Path(store).name == "datacube.zarr"
        assert Path(store).exists()
        arr = _zarr_array(store, "viirs", "flood_fraction")
        assert arr.shape[1:] == (grid.GLOBAL_HEIGHT, grid.GLOBAL_WIDTH)
        assert arr.chunks == (1, 256, 256)
        assert arr.shards == (1, 2048, 2048)

    def test_write_empty_dataset_raises(self, tmp_path):
        import xarray as xr

        with pytest.raises(ValueError, match="Dataset is empty"):
            ArchiveWriter(tmp_path).write(xr.Dataset(), "viirs", time=date(2020, 1, 1))

    def test_write_requires_time_or_event(self, tmp_path, simple_dataset):
        with pytest.raises(ValueError, match="requires `time`"):
            ArchiveWriter(tmp_path).write(simple_dataset, "viirs")

    def test_write_rejects_unaligned(self, tmp_path):
        import xarray as xr

        y = np.linspace(40.0, 20.0, 50)
        x = np.linspace(10.0, 30.0, 60)
        ds = xr.Dataset(
            {"flood_fraction": xr.DataArray(np.zeros((50, 60), "float32"), dims=["y", "x"], coords={"y": y, "x": x})}
        )
        with pytest.raises(ValueError, match="not aligned"):
            ArchiveWriter(tmp_path).write(ds, "viirs", time=date(2020, 1, 1))

    def test_write_without_masks_stores_flood_only(self, tmp_path, simple_dataset):
        import zarr

        store = ArchiveWriter(tmp_path).write(simple_dataset, "viirs", time=date(2020, 1, 1))
        group = zarr.open_group(store, mode="r")["viirs"]
        assert "flood_fraction" in group
        assert "quality_mask" not in group
        assert "permanent_water" not in group

    def test_write_ensure_masks_generates_channels(self, tmp_path, simple_dataset):
        import zarr

        store = ArchiveWriter(tmp_path).write(simple_dataset, "viirs", time=date(2020, 1, 1), ensure_masks=True)
        group = zarr.open_group(store, mode="r")["viirs"]
        assert "quality_mask" in group
        assert "permanent_water" in group

    def test_daily_write_bounded_provenance_no_bookmark(self, tmp_path, simple_dataset):
        import zarr

        store = ArchiveWriter(tmp_path).write(simple_dataset, "viirs", time=date(2020, 1, 1))
        attrs = dict(zarr.open_group(store, mode="r")["viirs"].attrs)
        assert attrs["source_id"] == "viirs"
        assert "last_updated" in attrs
        # Daily writes never pile up the event registry.
        assert attrs["atlantis_events"] == {}


# ── ArchiveReader (datacube) ──────────────────────────────────────────────────


class TestArchiveReader:
    def test_init(self, tmp_path):
        assert ArchiveReader(tmp_path).archive_root == str(tmp_path)

    def test_read_missing_store_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Datacube not found"):
            ArchiveReader(tmp_path).read("viirs", bbox=window_bbox())

    def test_read_by_bbox_cf_decode(self, tmp_path):
        ArchiveWriter(tmp_path).write(aligned_dataset(0.5), "viirs", time=date(2020, 1, 1))
        ds = ArchiveReader(tmp_path).read("viirs", bbox=window_bbox())
        assert ds.sizes["y"] == _H and ds.sizes["x"] == _W
        # uint8 50 decodes via scale_factor 0.01 -> 0.5
        np.testing.assert_allclose(float(ds["flood_fraction"].mean()), 0.5, atol=1e-6)

    def test_read_full_grid_when_no_bbox(self, tmp_path):
        ArchiveWriter(tmp_path).write(aligned_dataset(0.5), "viirs", time=date(2020, 1, 1))
        ds = ArchiveReader(tmp_path).read("viirs")
        assert ds.sizes["y"] == grid.GLOBAL_HEIGHT and ds.sizes["x"] == grid.GLOBAL_WIDTH

    def test_read_time_range(self, tmp_path):
        writer = ArchiveWriter(tmp_path)
        writer.write(aligned_dataset(0.3), "viirs", time=date(2020, 1, 1))
        writer.write(aligned_dataset(0.7), "viirs", time=date(2020, 1, 3))
        reader = ArchiveReader(tmp_path)
        assert reader.read("viirs", bbox=window_bbox()).sizes["time"] == 2
        one = reader.read("viirs", bbox=window_bbox(), start=date(2020, 1, 3), end=date(2020, 1, 3))
        assert one.sizes["time"] == 1
        np.testing.assert_allclose(float(one["flood_fraction"].mean()), 0.7, atol=1e-6)

    def test_read_resolves_crs(self, tmp_path):
        import rioxarray  # noqa: F401  (registers the .rio accessor)

        ArchiveWriter(tmp_path).write(aligned_dataset(0.5), "viirs", time=date(2020, 1, 1))
        ds = ArchiveReader(tmp_path).read("viirs", bbox=window_bbox())
        assert ds.rio.crs is not None
        assert ds.rio.crs.to_epsg() == 4326

    def test_sparse_unwritten_is_nodata(self, tmp_path):
        import xarray as xr

        store = ArchiveWriter(tmp_path).write(aligned_dataset(0.5), "viirs", time=date(2020, 1, 1))
        full = xr.open_zarr(store, group="viirs", consolidated=True)
        # A pixel far outside the AOI window decodes to NaN (chunk never written).
        assert np.isnan(float(full["flood_fraction"].isel(time=0, y=0, x=0)))

    def test_list_sources(self, tmp_path):
        writer = ArchiveWriter(tmp_path)
        writer.write(aligned_dataset(0.5), "viirs", time=date(2020, 1, 1))
        writer.write(aligned_dataset(0.5), "gfm", time=date(2020, 1, 1))
        assert ArchiveReader(tmp_path).list_sources() == ["gfm", "viirs"]

    def test_list_empty(self, tmp_path):
        reader = ArchiveReader(tmp_path)
        assert reader.list_sources() == []
        assert reader.list_events() == []

    def test_read_tiles_by_bbox(self, tmp_path):
        cfg = ArchiveConfig(chunk_size=32, shard_size=64)
        ArchiveWriter(tmp_path, cfg).write(aligned_dataset(0.5, h=64, w=64), "viirs", time=date(2020, 1, 1))
        out = ArchiveReader(tmp_path, cfg).read("viirs", bbox=window_bbox(h=64, w=64), tiles=[(0, 0), (1, 1)])
        assert out.sizes["tile"] == 2
        assert out.sizes["y"] == 32 and out.sizes["x"] == 32


# ── Optional event bookmarks ──────────────────────────────────────────────────


class TestEventBookmark:
    def test_write_event_registers_bookmark(self, tmp_path, event, simple_dataset):
        ArchiveWriter(tmp_path).write(simple_dataset, "viirs", event=event)
        assert ArchiveReader(tmp_path).list_events() == ["test_event"]

    def test_read_by_event(self, tmp_path, event):
        ArchiveWriter(tmp_path).write(aligned_dataset(0.5), "viirs", time=date(2020, 1, 1), event=event)
        ds = ArchiveReader(tmp_path).read("viirs", event="test_event")
        assert ds.sizes["y"] == _H and ds.sizes["x"] == _W
        np.testing.assert_allclose(float(ds["flood_fraction"].mean()), 0.5, atol=1e-6)

    def test_read_unknown_event_raises(self, tmp_path, event, simple_dataset):
        ArchiveWriter(tmp_path).write(simple_dataset, "viirs", event=event)
        with pytest.raises(KeyError):
            ArchiveReader(tmp_path).read("viirs", event="nope")


# ── ArchiveWriterCheckpoint (unchanged) ───────────────────────────────────────


class TestArchiveWriterCheckpoint:
    def test_write_checkpoint_creates_file(self, tmp_path):
        from datetime import date

        writer = ArchiveWriter(tmp_path)
        event = FloodEvent(
            event_id="event_001",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["viirs"],
        )
        path = writer.write_checkpoint(event, "viirs", "fetch")
        assert path.exists()
        assert path.name == "viirs_fetch.done"

    def test_is_checkpointed_true(self, tmp_path):
        from datetime import date

        writer = ArchiveWriter(tmp_path)
        event = FloodEvent(
            event_id="event_001",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["viirs"],
        )
        writer.write_checkpoint(event, "viirs", "fetch")
        assert writer.is_checkpointed(event, "viirs", "fetch") is True

    def test_is_checkpointed_false(self, tmp_path):
        from datetime import date

        writer = ArchiveWriter(tmp_path)
        event = FloodEvent(
            event_id="event_001",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["viirs"],
        )
        assert writer.is_checkpointed(event, "viirs", "harmonise") is False

    def test_multiple_stages_independent(self, tmp_path):
        from datetime import date

        writer = ArchiveWriter(tmp_path)
        event = FloodEvent(
            event_id="event_002",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["viirs"],
        )
        writer.write_checkpoint(event, "viirs", "fetch")
        assert writer.is_checkpointed(event, "viirs", "fetch") is True
        assert writer.is_checkpointed(event, "viirs", "harmonise") is False
        assert writer.is_checkpointed(event, "viirs", "archive") is False

        writer.write_checkpoint(event, "viirs", "harmonise")
        assert writer.is_checkpointed(event, "viirs", "harmonise") is True

    def test_write_checkpoint_returns_path(self, tmp_path):
        from datetime import date

        writer = ArchiveWriter(tmp_path)
        event = FloodEvent(
            event_id="event_003",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["viirs"],
        )
        result = writer.write_checkpoint(event, "viirs", "fetch")
        assert isinstance(result, Path)
        assert result.parent.exists()
