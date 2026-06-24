"""Tests for archive reader and writer."""

from datetime import date
from pathlib import Path

import numpy as np
import pytest

from atlantis.archive.reader import ArchiveReader
from atlantis.archive.writer import ArchiveWriter
from atlantis.models.event import FloodEvent

# ── Fixtures ──────────────────────────────────────────────────────────────────


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
    import xarray as xr

    y = np.linspace(40.0, 20.0, 50)
    x = np.linspace(10.0, 30.0, 60)
    data = np.random.rand(50, 60).astype("float32")
    return xr.Dataset(
        {"flood_fraction": xr.DataArray(data, dims=["y", "x"], coords={"y": y, "x": x})},
        attrs={"crs": "EPSG:4326"},
    )


# ── ArchiveReader ─────────────────────────────────────────────────────────────


class TestArchiveReader:
    def test_init(self, tmp_path):
        reader = ArchiveReader(tmp_path)
        assert reader.archive_root == tmp_path

    def test_read_raw_missing_raises(self, tmp_path):
        reader = ArchiveReader(tmp_path)
        with pytest.raises(FileNotFoundError, match="Raw archive not found"):
            reader.read_raw("missing_event", "viirs")

    def test_read_ml_ready_missing_raises(self, tmp_path):
        reader = ArchiveReader(tmp_path)
        with pytest.raises(FileNotFoundError, match="ML-ready archive not found"):
            reader.read_ml_ready("missing_event", "viirs")

    def test_list_events_empty(self, tmp_path):
        reader = ArchiveReader(tmp_path)
        assert reader.list_events() == []

    def test_list_sources_empty(self, tmp_path):
        reader = ArchiveReader(tmp_path)
        assert reader.list_sources("nonexistent") == []

    def test_list_events_after_write(self, tmp_path, event, simple_dataset):
        writer = ArchiveWriter(tmp_path)
        writer.write_raw(simple_dataset, event, "viirs")

        reader = ArchiveReader(tmp_path)
        events = reader.list_events()
        assert "test_event" in events

    def test_list_sources_after_write(self, tmp_path, event, simple_dataset):
        writer = ArchiveWriter(tmp_path)
        writer.write_raw(simple_dataset, event, "viirs")
        writer.write_raw(simple_dataset, event, "gfm")

        reader = ArchiveReader(tmp_path)
        sources = reader.list_sources("test_event")
        assert "viirs" in sources
        assert "gfm" in sources

    def test_read_raw_roundtrip(self, tmp_path, event, simple_dataset):
        writer = ArchiveWriter(tmp_path)
        writer.write_raw(simple_dataset, event, "viirs")

        reader = ArchiveReader(tmp_path)
        ds = reader.read_raw("test_event", "viirs")
        assert "flood_fraction" in ds.data_vars
        np.testing.assert_allclose(
            ds["flood_fraction"].values,
            simple_dataset["flood_fraction"].values,
            rtol=1e-5,
        )

    def test_read_ml_ready_roundtrip(self, tmp_path, event, simple_dataset):
        writer = ArchiveWriter(tmp_path)
        writer.write_ml_ready(simple_dataset, event, "viirs")

        reader = ArchiveReader(tmp_path)
        ds = reader.read_ml_ready("test_event", "viirs")
        assert "flood_fraction" in ds.data_vars
        assert "quality_mask" in ds.data_vars
        assert "permanent_water" in ds.data_vars

    def test_read_ml_ready_tile_selection(self, tmp_path, event):
        """Selecting tiles returns only the requested spatial window."""
        import xarray as xr

        from atlantis.config import HarmoniseConfig

        tile_size = 32
        cfg = HarmoniseConfig(tile_size=tile_size)

        y = np.arange(128, dtype="float32")
        x = np.arange(128, dtype="float32")
        data = np.random.rand(128, 128).astype("float32")
        ds = xr.Dataset({"flood_fraction": xr.DataArray(data, dims=["y", "x"], coords={"y": y, "x": x})})

        writer = ArchiveWriter(tmp_path)
        writer.write_ml_ready(ds, event, "viirs", harmonise_config=cfg)

        reader = ArchiveReader(tmp_path)
        # Request tile (0, 0): rows 0..31, cols 0..31
        result = reader.read_ml_ready("test_event", "viirs", tiles=[(0, 0)])
        assert result.sizes["y"] == tile_size
        assert result.sizes["x"] == tile_size


# ── ArchiveWriter ─────────────────────────────────────────────────────────────


class TestArchiveWriter:
    def test_init(self, tmp_path):
        writer = ArchiveWriter(tmp_path)
        assert writer.archive_root == tmp_path
        assert writer.raw_path == tmp_path / "raw"
        assert writer.ml_path == tmp_path / "ml-ready"

    def test_write_raw_returns_zarr_path(self, tmp_path, event, simple_dataset):
        writer = ArchiveWriter(tmp_path)
        zarr_path = writer.write_raw(simple_dataset, event, "viirs")
        assert zarr_path.exists()
        assert zarr_path.name == "data.zarr"

    def test_write_raw_creates_metadata(self, tmp_path, event, simple_dataset):
        import json

        writer = ArchiveWriter(tmp_path)
        zarr_path = writer.write_raw(simple_dataset, event, "viirs")
        metadata_path = zarr_path.parent / "metadata.json"
        assert metadata_path.exists()
        with open(metadata_path) as fh:
            meta = json.load(fh)
        assert meta["event_id"] == "test_event"
        assert meta["source_id"] == "viirs"
        assert meta["variables"] == ["flood_fraction"]

    def test_write_raw_empty_dataset_raises(self, tmp_path, event):
        import xarray as xr

        writer = ArchiveWriter(tmp_path)
        empty_ds = xr.Dataset()
        with pytest.raises(ValueError, match="Dataset is empty"):
            writer.write_raw(empty_ds, event, "viirs")

    def test_write_ml_ready_returns_zarr_path(self, tmp_path, event, simple_dataset):
        writer = ArchiveWriter(tmp_path)
        zarr_path = writer.write_ml_ready(simple_dataset, event, "viirs")
        assert zarr_path.exists()
        assert zarr_path.name == "data.zarr"

    def test_write_ml_ready_adds_quality_mask(self, tmp_path, event, simple_dataset):
        import xarray as xr

        writer = ArchiveWriter(tmp_path)
        writer.write_ml_ready(simple_dataset, event, "viirs")

        ds = xr.open_zarr(tmp_path / "ml-ready" / "test_event" / "viirs" / "data.zarr")
        assert "quality_mask" in ds.data_vars
        assert "permanent_water" in ds.data_vars

    def test_write_ml_ready_chunk_size(self, tmp_path, event):
        """Spatial chunks should equal tile_size (capped at dataset size)."""
        import xarray as xr

        from atlantis.config import HarmoniseConfig

        tile_size = 32
        cfg = HarmoniseConfig(tile_size=tile_size)
        y = np.arange(100, dtype="float32")
        x = np.arange(100, dtype="float32")
        data = np.random.rand(100, 100).astype("float32")
        ds = xr.Dataset({"flood_fraction": xr.DataArray(data, dims=["y", "x"], coords={"y": y, "x": x})})

        writer = ArchiveWriter(tmp_path)
        zarr_path = writer.write_ml_ready(ds, event, "viirs", harmonise_config=cfg)

        ds_out = xr.open_zarr(zarr_path)
        # Zarr stores the encoding; we verify via actual chunks
        assert ds_out.chunks["y"][0] == tile_size
        assert ds_out.chunks["x"][0] == tile_size

    def test_write_ml_ready_creates_metadata(self, tmp_path, event, simple_dataset):
        import json

        from atlantis.config import HarmoniseConfig

        writer = ArchiveWriter(tmp_path)
        cfg = HarmoniseConfig()
        zarr_path = writer.write_ml_ready(simple_dataset, event, "viirs", harmonise_config=cfg)
        metadata_path = zarr_path.parent / "metadata.json"
        assert metadata_path.exists()
        with open(metadata_path) as fh:
            meta = json.load(fh)
        assert meta["tile_size"] == cfg.tile_size
        assert "harmonise_config" in meta

    def test_write_raw_chunk_size(self, tmp_path, event):
        """Raw Zarr chunks must be at most 256 pixels (inclusive) in each spatial dim."""
        import xarray as xr

        y = np.arange(400, dtype="float32")
        x = np.arange(500, dtype="float32")
        data = np.random.rand(400, 500).astype("float32")
        ds = xr.Dataset({"flood_fraction": xr.DataArray(data, dims=["y", "x"], coords={"y": y, "x": x})})

        writer = ArchiveWriter(tmp_path)
        zarr_path = writer.write_raw(ds, event, "viirs")

        ds_out = xr.open_zarr(zarr_path)
        # _RAW_CHUNK_SIZE == 256; chunk may equal 256 for dims larger than 256.
        assert ds_out.chunks["y"][0] <= 256
        assert ds_out.chunks["x"][0] <= 256


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
