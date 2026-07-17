"""Unit tests for modis/inventory.py."""

from __future__ import annotations

import pandas as pd
import pytest

from atlantis.fetchers.modis.inventory import slice_partition, to_tasks


@pytest.fixture()
def sample_df():
    """Minimal catalogue-shaped DataFrame."""
    n = 20
    return pd.DataFrame(
        {
            "date": [f"2020-01-{i + 1:02d}" for i in range(n)],
            "h": [8] * n,
            "v": [5] * n,
            "task_id": [f"modis-task-{i:03d}" for i in range(n)],
            "source_uri": [f"https://ladsweb.modaps.eosdis.nasa.gov/tile-{i}.hdf" for i in range(n)],
        }
    )


def test_slice_partition_none_returns_all_sorted(sample_df):
    result = slice_partition(sample_df, None)
    assert len(result) == len(sample_df)
    dates = list(result["date"])
    assert dates == sorted(dates)


def test_slice_partition_basic(sample_df):
    result = slice_partition(sample_df, "0:10")
    assert len(result) == 10


def test_slice_partition_second_half(sample_df):
    first = slice_partition(sample_df, "0:10")
    second = slice_partition(sample_df, "10:20")
    combined_ids = set(first["task_id"]) | set(second["task_id"])
    assert combined_ids == set(sample_df["task_id"])


def test_slice_partition_invalid_format(sample_df):
    with pytest.raises(ValueError, match="start:stop"):
        slice_partition(sample_df, "bad")


def test_slice_partition_out_of_range(sample_df):
    with pytest.raises(ValueError, match="out of range"):
        slice_partition(sample_df, "0:999")


def test_to_tasks_shape(sample_df):
    tasks = to_tasks(sample_df)
    assert len(tasks) == len(sample_df)
    required_keys = {"task_id", "source_uri", "date", "h", "v"}
    for task in tasks:
        assert required_keys.issubset(task.keys())


def test_to_tasks_unique_task_ids(sample_df):
    tasks = to_tasks(sample_df)
    ids = [t["task_id"] for t in tasks]
    assert len(ids) == len(set(ids))


def test_to_tasks_hv_types(sample_df):
    tasks = to_tasks(sample_df)
    for task in tasks:
        assert isinstance(task["h"], int)
        assert isinstance(task["v"], int)
