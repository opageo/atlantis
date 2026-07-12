"""Tests for ArchiveReader internal helpers."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from atlantis.archive.reader import _to_dt64


class TestToDt64:
    """Tests for the _to_dt64 helper."""

    def test_none_returns_none(self) -> None:
        assert _to_dt64(None) is None

    def test_date_converts(self) -> None:
        result = _to_dt64(date(2024, 1, 15))
        assert result == np.datetime64("2024-01-15", "ns")

    def test_string_converts(self) -> None:
        result = _to_dt64("2024-01-15")
        assert result == np.datetime64("2024-01-15", "ns")


class TestArchiveReadMethods:
    """Tests for edge cases in ArchiveReader methods."""

    def test_group_attrs_handles_exception(self) -> None:
        from atlantis.archive.reader import ArchiveReader

        reader = ArchiveReader.__new__(ArchiveReader)
        reader._store = MagicMock(return_value=Path("/fake"))

        with patch("zarr.open_group", side_effect=RuntimeError("boom")):
            result = reader._group_attrs("viirs")
            assert result == {}

    def test_group_names_handles_missing_store(self) -> None:
        from atlantis.archive.reader import ArchiveReader

        reader = MagicMock(spec=ArchiveReader)
        reader._store.return_value = Path("/nonexistent")
        reader._group_names = ArchiveReader._group_names.__get__(reader, ArchiveReader)

        with patch("atlantis.archive.reader.store_for", return_value=Path("/fake/cube.zarr")):
            result = reader._group_names()
            assert isinstance(result, list)

    def test_group_names_handles_exception(self) -> None:
        from atlantis.archive.reader import ArchiveReader

        reader = ArchiveReader.__new__(ArchiveReader)
        reader._store = MagicMock(return_value=Path("/fake"))

        with patch("zarr.open_group", side_effect=RuntimeError("boom")):
            result = reader._group_names()
            assert result == []


class TestWriterHelpers:
    """Tests for archive writer utility functions."""

    def test_find_dim_returns_none_when_not_found(self) -> None:
        import numpy as np
        import xarray as xr

        from atlantis.archive.writer import _find_dim

        ds = xr.Dataset({"v": xr.DataArray(np.zeros((3, 3)), dims=["a", "b"])})
        assert _find_dim(ds, ("y", "x")) is None

    def test_find_dim_returns_match(self) -> None:
        import numpy as np
        import xarray as xr

        from atlantis.archive.writer import _find_dim

        ds = xr.Dataset({"v": xr.DataArray(np.zeros((3, 3)), dims=["y", "x"])})
        assert _find_dim(ds, ("y", "x")) == "y"

    def test_ensure_time_dim_preserves_existing(self) -> None:
        import numpy as np
        import xarray as xr

        from atlantis.archive.writer import ArchiveWriter

        writer = ArchiveWriter.__new__(ArchiveWriter)
        writer._as_date = lambda t: np.datetime64(t, "ns")

        ds = xr.Dataset({"v": xr.DataArray(np.zeros(3), dims=["time"])})
        ds["time"] = [np.datetime64("2024-01-01"), np.datetime64("2024-01-02"), np.datetime64("2024-01-03")]
        result = writer._ensure_time_dim(ds, time=None, event=None)
        assert result is ds
