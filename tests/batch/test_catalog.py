"""Unit tests for atlantis.batch.catalog (shared batch-catalogue core)."""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from atlantis.batch.catalog import (
    iter_dates,
    load_catalogue,
    log_progress,
    retry_request,
    slice_partition,
    write_catalogue,
)


def test_iter_dates_inclusive():
    days = list(iter_dates("2024-01-01", "2024-01-03"))
    assert days == [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)]


def test_iter_dates_single_day():
    days = list(iter_dates("2024-01-01", "2024-01-01"))
    assert days == [date(2024, 1, 1)]


@pytest.fixture()
def sample_df():
    n = 20
    return pd.DataFrame(
        {
            "date": [f"2020-01-{i + 1:02d}" for i in range(n)],
            "aoi_id": list(range(1, n + 1)),
        }
    )


def test_slice_partition_none_returns_all_sorted(sample_df):
    result = slice_partition(sample_df, None, ("date", "aoi_id"))
    assert len(result) == len(sample_df)
    dates = list(result["date"])
    assert dates == sorted(dates)


def test_slice_partition_basic(sample_df):
    result = slice_partition(sample_df, "0:10", ("date", "aoi_id"))
    assert len(result) == 10


def test_slice_partition_second_half(sample_df):
    first = slice_partition(sample_df, "0:10", ("date", "aoi_id"))
    second = slice_partition(sample_df, "10:20", ("date", "aoi_id"))
    combined_ids = set(first["aoi_id"]) | set(second["aoi_id"])
    assert combined_ids == set(sample_df["aoi_id"])


def test_slice_partition_invalid_format(sample_df):
    with pytest.raises(ValueError, match="start:stop"):
        slice_partition(sample_df, "bad", ("date", "aoi_id"))


def test_slice_partition_out_of_range(sample_df):
    with pytest.raises(ValueError, match="out of range"):
        slice_partition(sample_df, "0:999", ("date", "aoi_id"))


def test_slice_partition_uses_given_sort_keys():
    df = pd.DataFrame({"date": ["2020-01-02", "2020-01-01"], "h": [1, 1], "v": [9, 5]})
    result = slice_partition(df, None, ("date", "h", "v"))
    assert list(result["date"]) == ["2020-01-01", "2020-01-02"]


def test_write_catalogue_local_creates_parent(tmp_path):
    df = pd.DataFrame({"a": [1, 2, 3]})
    output = tmp_path / "sub" / "catalog.parquet"
    result = write_catalogue(df, output)
    assert result == output
    assert output.exists()
    assert len(pd.read_parquet(output)) == 3


def test_load_catalogue_local(tmp_path):
    df = pd.DataFrame({"a": [1, 2, 3]})
    output = tmp_path / "catalog.parquet"
    df.to_parquet(output)
    loaded = load_catalogue(output)
    assert len(loaded) == 3


def test_retry_request_succeeds_first_try():
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    assert retry_request(fn, max_retries=3) == "ok"
    assert len(calls) == 1


def test_retry_request_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("atlantis.batch.catalog.time.sleep", lambda _: None)
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ValueError("transient")
        return "ok"

    assert retry_request(fn, max_retries=5, backoff_base=0.01) == "ok"
    assert attempts["n"] == 3


def test_retry_request_raises_after_max_retries(monkeypatch):
    monkeypatch.setattr("atlantis.batch.catalog.time.sleep", lambda _: None)

    def fn():
        raise ValueError("permanent")

    with pytest.raises(ValueError, match="permanent"):
        retry_request(fn, max_retries=3, backoff_base=0.01)


def test_retry_request_only_catches_specified_exceptions():
    def fn():
        raise KeyError("boom")

    with pytest.raises(KeyError):
        retry_request(fn, max_retries=3, exceptions=(ValueError,))


def test_write_catalogue_routes_raw_s3_string_to_s3_branch():
    """Regression test: passing an s3:// URI must never go through Path().

    ``Path("s3://bucket/key")`` collapses the double slash to a single one
    (``"s3:/bucket/key"``), which breaks the ``.startswith("s3://")`` check in
    ``write_catalogue``. Callers (CLI commands) must pass the raw string
    straight through — this test guards against that regression by mocking
    s3fs and asserting it is actually invoked for a genuine ``s3://`` URI.
    """
    df = pd.DataFrame({"a": [1, 2, 3]})
    with patch("s3fs.S3FileSystem") as mock_fs_cls:
        mock_fs = MagicMock()
        mock_fs.open.return_value.__enter__.return_value = io.BytesIO()
        mock_fs_cls.return_value = mock_fs
        result = write_catalogue(
            df, "s3://atlantis/assets/modis/x.parquet", storage_options={"endpoint_url": "https://example.test"}
        )

    mock_fs_cls.assert_called_once_with(endpoint_url="https://example.test")
    mock_fs.open.assert_called_once_with("s3://atlantis/assets/modis/x.parquet", "wb")
    assert result is None


def test_path_wrapping_an_s3_uri_corrupts_it():
    """Documents the exact pitfall test_write_catalogue_routes_raw_s3_string_to_s3_branch guards against."""
    corrupted = str(Path("s3://atlantis/assets/modis/x.parquet"))
    assert corrupted == "s3:/atlantis/assets/modis/x.parquet"
    assert not corrupted.startswith("s3://")


def test_log_progress_emits_every_n(monkeypatch):
    lines = []
    monkeypatch.setattr("atlantis.batch.catalog.logger.info", lambda *a: lines.append(a))
    for i in range(65):
        log_progress(i, 65, every=30, label="test")
    # Emits at 30, 60, and the final 65 (last item), not on every iteration.
    assert len(lines) == 3


def test_log_progress_silent_between_intervals(monkeypatch):
    lines = []
    monkeypatch.setattr("atlantis.batch.catalog.logger.info", lambda *a: lines.append(a))
    for i in range(5):
        log_progress(i, 100, every=30, label="test")
    assert len(lines) == 0


def test_log_progress_on_progress_bypasses_logger(monkeypatch):
    """Regression test: the CLI disables loguru output entirely unless --verbose

    is passed (``logger.disable("atlantis")`` in ``atlantis.cli._main``), which
    would silently swallow ``logger.info`` progress lines. ``on_progress`` must
    let callers route around that.
    """
    logger_calls = []
    monkeypatch.setattr("atlantis.batch.catalog.logger.info", lambda *a: logger_calls.append(a))
    sink_calls = []
    log_progress(29, 65, every=30, label="test", on_progress=sink_calls.append)
    assert logger_calls == []
    assert sink_calls == ["test: 30/65 (46.2%)"]
