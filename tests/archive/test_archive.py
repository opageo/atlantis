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


@pytest.fixture()
def event() -> FloodEvent:
    return FloodEvent(
        event_id="test_event",
        bbox=(10.0, 20.0, 30.0, 40.0),
        start_date=date(2020, 1, 1),
        end_date=date(2020, 1, 5),
        sources=["viirs"],
    )


@pytest.fixture()
def simple_dataset():
    return aligned_dataset(0.5)


# ── ArchiveWriter (datacube) ──────────────────────────────────────────────────


class TestArchiveWriter:
    def test_write_raw_creates_consolidated_cube(self, tmp_path, event, simple_dataset):
        store = ArchiveWriter(tmp_path).write_raw(simple_dataset, event, "viirs")
        # A single consolidated store (not a per-event store), grouped by source.
        assert Path(store).name == "raw.zarr"
        assert Path(store).exists()
        arr = _zarr_array(store, "viirs", "flood_fraction")
        assert arr.shape[1:] == (grid.GLOBAL_HEIGHT, grid.GLOBAL_WIDTH)

    def test_write_raw_empty_dataset_raises(self, tmp_path, event):
        import xarray as xr

        with pytest.raises(ValueError, match="Dataset is empty"):
            ArchiveWriter(tmp_path).write_raw(xr.Dataset(), event, "viirs")

    def test_write_raw_rejects_unaligned(self, tmp_path, event):
        import xarray as xr

        y = np.linspace(40.0, 20.0, 50)
        x = np.linspace(10.0, 30.0, 60)
        ds = xr.Dataset({
            "flood_fraction": xr.DataArray(np.zeros((50, 60), "float32"), dims=["y", "x"], coords={"y": y, "x": x})
        })
        with pytest.raises(ValueError, match="not aligned"):
            ArchiveWriter(tmp_path).write_raw(ds, event, "viirs")

    def test_raw_chunking_unsharded(self, tmp_path, event, simple_dataset):
        store = ArchiveWriter(tmp_path).write_raw(simple_dataset, event, "viirs")
        arr = _zarr_array(store, "viirs", "flood_fraction")
        assert arr.chunks == (1, 1024, 1024)
        assert arr.shards is None

    def test_ml_ready_sharded_and_masked(self, tmp_path, event, simple_dataset):
        import zarr

        store = ArchiveWriter(tmp_path).write_ml_ready(simple_dataset, event, "viirs")
        assert Path(store).name == "ml-ready.zarr"
        arr = _zarr_array(store, "viirs", "flood_fraction")
        assert arr.chunks == (1, 256, 256)
        assert arr.shards == (1, 2048, 2048)
        group = zarr.open_group(store, mode="r")["viirs"]
        assert "quality_mask" in group
        assert "permanent_water" in group


# ── ArchiveReader (datacube) ──────────────────────────────────────────────────


class TestArchiveReader:
    def test_init(self, tmp_path):
        assert ArchiveReader(tmp_path).archive_root == str(tmp_path)

    def test_read_raw_missing_store_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Raw datacube not found"):
            ArchiveReader(tmp_path).read_raw("missing_event", "viirs")

    def test_read_ml_ready_missing_store_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="ML-ready datacube not found"):
            ArchiveReader(tmp_path).read_ml_ready("missing_event", "viirs")

    def test_read_raw_unknown_event_raises(self, tmp_path, event, simple_dataset):
        ArchiveWriter(tmp_path).write_raw(simple_dataset, event, "viirs")
        with pytest.raises(KeyError):
            ArchiveReader(tmp_path).read_raw("nope", "viirs")

    def test_read_raw_roundtrip_cf_decode(self, tmp_path, event):
        ArchiveWriter(tmp_path).write_raw(aligned_dataset(0.5), event, "viirs", time=date(2020, 1, 1))
        ds = ArchiveReader(tmp_path).read_raw("test_event", "viirs")
        assert ds.sizes["y"] == _H and ds.sizes["x"] == _W
        # uint8 50 decodes via scale_factor 0.01 -> 0.5
        np.testing.assert_allclose(float(ds["flood_fraction"].mean()), 0.5, atol=1e-6)

    def test_read_raw_resolves_crs(self, tmp_path, event, simple_dataset):
        import rioxarray  # noqa: F401  (registers the .rio accessor)

        ArchiveWriter(tmp_path).write_raw(simple_dataset, event, "viirs")
        ds = ArchiveReader(tmp_path).read_raw("test_event", "viirs")
        assert ds.rio.crs is not None
        assert ds.rio.crs.to_epsg() == 4326

    def test_read_raw_multiple_dates(self, tmp_path, event):
        writer = ArchiveWriter(tmp_path)
        writer.write_raw(aligned_dataset(0.3), event, "viirs", time=date(2020, 1, 1))
        writer.write_raw(aligned_dataset(0.7), event, "viirs", time=date(2020, 1, 3))
        ds = ArchiveReader(tmp_path).read_raw("test_event", "viirs")
        assert ds.sizes["time"] == 2

    def test_sparse_unwritten_is_nodata(self, tmp_path, event, simple_dataset):
        import xarray as xr

        store = ArchiveWriter(tmp_path).write_raw(simple_dataset, event, "viirs")
        full = xr.open_zarr(store, group="viirs", consolidated=True)
        # A pixel far outside the AOI window decodes to NaN (chunk never written).
        assert np.isnan(float(full["flood_fraction"].isel(time=0, y=0, x=0)))

    def test_list_events_and_sources(self, tmp_path, event, simple_dataset):
        writer = ArchiveWriter(tmp_path)
        writer.write_raw(simple_dataset, event, "viirs")
        writer.write_raw(simple_dataset, event, "gfm")
        reader = ArchiveReader(tmp_path)
        assert "test_event" in reader.list_events()
        assert reader.list_sources("test_event") == ["gfm", "viirs"]

    def test_list_empty(self, tmp_path):
        reader = ArchiveReader(tmp_path)
        assert reader.list_events() == []
        assert reader.list_sources("nonexistent") == []

    def test_read_ml_ready_tile_selection(self, tmp_path, event):
        cfg = ArchiveConfig(ml_tile_size=32, ml_shard_size=64)
        ArchiveWriter(tmp_path, cfg).write_ml_ready(aligned_dataset(0.5, h=64, w=64), event, "viirs")
        out = ArchiveReader(tmp_path, cfg).read_ml_ready("test_event", "viirs", tiles=[(0, 0), (1, 1)])
        assert out.sizes["tile"] == 2
        assert out.sizes["y"] == 32 and out.sizes["x"] == 32


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
