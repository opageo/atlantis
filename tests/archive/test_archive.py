"""Tests for archive reader and writer."""

from datetime import date
from pathlib import Path

import numpy as np
import pytest

from atlantis.archive.reader import ArchiveReader
from atlantis.archive.writer import ArchiveWriter
from atlantis.models.event import FloodEvent


class TestArchiveReader:
    def test_init(self, tmp_path):
        reader = ArchiveReader(tmp_path)
        assert reader.archive_root == tmp_path

    def test_read_raw_not_implemented(self, tmp_path):
        reader = ArchiveReader(tmp_path)
        with pytest.raises(NotImplementedError, match="Raw archive reading not yet implemented"):
            reader.read_raw("event_001", "viirs")

    def test_read_ml_ready_not_implemented(self, tmp_path):
        reader = ArchiveReader(tmp_path)
        with pytest.raises(NotImplementedError, match="ML-ready archive reading not yet implemented"):
            reader.read_ml_ready("event_001", "viirs")

    def test_list_events_not_implemented(self, tmp_path):
        reader = ArchiveReader(tmp_path)
        with pytest.raises(NotImplementedError, match="Event listing not yet implemented"):
            reader.list_events()

    def test_list_sources_not_implemented(self, tmp_path):
        reader = ArchiveReader(tmp_path)
        with pytest.raises(NotImplementedError, match="Source listing not yet implemented"):
            reader.list_sources("event_001")


class TestArchiveWriter:
    def test_init(self, tmp_path):
        writer = ArchiveWriter(tmp_path)
        assert writer.archive_root == tmp_path
        assert writer.raw_path == tmp_path / "raw"
        assert writer.ml_path == tmp_path / "ml-ready"

    def test_write_raw_not_implemented(self, tmp_path):
        import xarray as xr

        writer = ArchiveWriter(tmp_path)
        ds = xr.Dataset({"flood_extent": xr.DataArray(np.zeros((10, 10)))})
        event = FloodEvent(
            event_id="test_event",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["viirs"],
        )
        with pytest.raises(NotImplementedError, match="Raw archive writing not yet implemented"):
            writer.write_raw(ds, event, "viirs")

    def test_write_ml_ready_not_implemented(self, tmp_path):
        import xarray as xr

        writer = ArchiveWriter(tmp_path)
        ds = xr.Dataset({"flood_extent": xr.DataArray(np.zeros((10, 10)))})
        event = FloodEvent(
            event_id="test_event",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["viirs"],
        )
        with pytest.raises(NotImplementedError, match="ML-ready archive writing not yet implemented"):
            writer.write_ml_ready(ds, event, "viirs")


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
