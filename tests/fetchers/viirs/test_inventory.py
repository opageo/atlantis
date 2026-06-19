"""Unit tests for viirs/inventory.py."""

import pandas as pd
import pytest

from atlantis.fetchers.viirs.inventory import slice_partition, to_tasks


@pytest.fixture()
def sample_df():
    """Minimal catalogue-shaped DataFrame."""
    n = 20
    return pd.DataFrame(
        {
            "date": [f"2020-01-{i + 1:02d}" for i in range(n)],
            "aoi_id": list(range(1, n + 1)),
            "s3_key": [
                f"JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/01/{i + 1:02d}/"
                f"VIIRS-Flood-1day-GLB{i + 1:03d}_v1r0_blend_s2020010100000_e2020010123000_c20220809000.tif"
                for i in range(n)
            ],
            "geometry": [b"\x00" * 10] * n,
        }
    )


def test_slice_partition_none_returns_all_sorted(sample_df):
    result = slice_partition(sample_df, None)
    assert len(result) == len(sample_df)
    # Verify sorted
    dates = list(result["date"])
    assert dates == sorted(dates)


def test_slice_partition_basic(sample_df):
    result = slice_partition(sample_df, "0:10")
    assert len(result) == 10


def test_slice_partition_second_half(sample_df):
    first = slice_partition(sample_df, "0:10")
    second = slice_partition(sample_df, "10:20")
    combined_ids = set(first["aoi_id"]) | set(second["aoi_id"])
    all_ids = set(sample_df["aoi_id"])
    assert combined_ids == all_ids


def test_slice_partition_invalid_format(sample_df):
    with pytest.raises(ValueError, match="start:stop"):
        slice_partition(sample_df, "bad")


def test_slice_partition_out_of_range(sample_df):
    with pytest.raises(ValueError, match="out of range"):
        slice_partition(sample_df, "0:999")


def test_to_tasks_shape(sample_df):
    tasks = to_tasks(sample_df)
    assert len(tasks) == len(sample_df)
    required_keys = {"task_id", "source_uri", "dest_key", "date", "aoi_id"}
    for task in tasks:
        assert required_keys.issubset(task.keys())


def test_to_tasks_unique_task_ids(sample_df):
    tasks = to_tasks(sample_df)
    ids = [t["task_id"] for t in tasks]
    assert len(ids) == len(set(ids))


def test_to_tasks_source_uri_format(sample_df):
    tasks = to_tasks(sample_df)
    for task in tasks:
        assert task["source_uri"].startswith("https://noaa-jpss.s3.amazonaws.com/")


def test_to_tasks_dest_key_format(sample_df):
    tasks = to_tasks(sample_df)
    for task in tasks:
        # e.g. viirs/jpss/2020/2020-01-01/GLB001.tif
        assert task["dest_key"].endswith(".tif")
        assert "GLB" in task["dest_key"]


def test_to_tasks_no_geometry_key(sample_df):
    tasks = to_tasks(sample_df)
    for task in tasks:
        assert "geometry" not in task
