"""Unit tests for gfm/inventory.py."""

from __future__ import annotations

import pandas as pd
import pytest

from atlantis.fetchers.gfm.inventory import slice_partition, to_tasks

_HREF = "https://stac.eodc.eu/api/v1/collections/GFM/items/{}"


@pytest.fixture()
def sample_df():
    """Catalogue-shaped DataFrame: 2 items share one (date, tile) cell, 1 is distinct."""
    return pd.DataFrame(
        {
            "date": ["2024-11-01", "2024-11-01", "2024-11-02"],
            "equi7_tile": ["EU020M_E036N009T3", "EU020M_E036N009T3", "EU020M_E036N006T3"],
            "item_id": ["a", "b", "c"],
            "item_href": [_HREF.format("a"), _HREF.format("b"), _HREF.format("c")],
            "west": [1.0, 1.5, 5.0],
            "south": [2.0, 2.5, 6.0],
            "east": [3.0, 3.5, 7.0],
            "north": [4.0, 4.5, 8.0],
        }
    )


def test_slice_partition_none_returns_all_sorted(sample_df):
    result = slice_partition(sample_df, None)
    assert len(result) == len(sample_df)
    dates = list(result["date"])
    assert dates == sorted(dates)


def test_slice_partition_basic(sample_df):
    result = slice_partition(sample_df, "0:2")
    assert len(result) == 2


def test_slice_partition_invalid_format(sample_df):
    with pytest.raises(ValueError, match="start:stop"):
        slice_partition(sample_df, "bad")


def test_slice_partition_out_of_range(sample_df):
    with pytest.raises(ValueError, match="out of range"):
        slice_partition(sample_df, "0:999")


def test_to_tasks_groups_items_sharing_a_cell(sample_df):
    tasks = to_tasks(sample_df)
    # 2 rows share (2024-11-01, EU020M_E036N009T3) -> collapse into 1 task.
    assert len(tasks) == 2
    grouped = next(t for t in tasks if t["equi7_tile"] == "EU020M_E036N009T3")
    assert grouped["date"] == "2024-11-01"
    assert set(grouped["item_hrefs"]) == {_HREF.format("a"), _HREF.format("b")}
    # bbox is the union of the group's item bboxes.
    assert grouped["bbox"] == (1.0, 2.0, 3.5, 4.5)


def test_to_tasks_distinct_cells_stay_separate(sample_df):
    tasks = to_tasks(sample_df)
    ids = {t["task_id"] for t in tasks}
    assert ids == {"gfm-20241101-EU020M_E036N009T3", "gfm-20241102-EU020M_E036N006T3"}


def test_to_tasks_single_item_cell_has_one_href(sample_df):
    tasks = to_tasks(sample_df)
    solo = next(t for t in tasks if t["equi7_tile"] == "EU020M_E036N006T3")
    assert solo["item_hrefs"] == [_HREF.format("c")]
    assert solo["bbox"] == (5.0, 6.0, 7.0, 8.0)


def test_to_tasks_task_id_unique(sample_df):
    tasks = to_tasks(sample_df)
    ids = [t["task_id"] for t in tasks]
    assert len(ids) == len(set(ids))


def test_to_tasks_required_keys(sample_df):
    tasks = to_tasks(sample_df)
    required = {"task_id", "date", "equi7_tile", "item_hrefs", "bbox"}
    for task in tasks:
        assert required.issubset(task.keys())
