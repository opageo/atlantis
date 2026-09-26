"""Tests for scripts/merge_yearly_zarr.py on small local yearly stores."""

import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest
import zarr

from atlantis.archive import datacube
from atlantis.archive.grid import IndexWindow

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "merge_yearly_zarr.py"
_spec = importlib.util.spec_from_file_location("merge_yearly_zarr", _SCRIPT)
merge = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = merge
_spec.loader.exec_module(merge)

EPOCH = "2020-01-01"
UNITS = datacube.epoch_units(EPOCH)
VARS = ["water_fraction", "exclusion_mask", "recurring_flood"]
DAYS_2019_2022 = 365 + 366 + 365 + 365


def _window(row: int, col: int, size: int = 64) -> IndexWindow:
    return IndexWindow(row_start=row, row_stop=row + size, col_start=col, col_stop=col + size)


def _make_year(root, year, writes, *, source="modis", prefill=True, stray=False, chunk=256, events=None):
    """Build ``root/<year>/datacube.zarr``; *writes* = [(date, var, (row, col), value)]."""
    store = Path(root) / str(year) / "datacube.zarr"
    g = datacube.ensure_source_group(
        datacube.open_root(store),
        source,
        VARS,
        chunk=chunk,
        shard=2048,
        scale_factor=0.01,
        time_units=UNITS,
        prefill_year=year if prefill else None,
    )
    time_arr, arrs = datacube.get_handles(g, VARS)
    if stray:
        # Legacy 366-slot prefill of a 365-day year: slot 365 is next year's 1 January.
        datacube.ensure_time_index(time_arr, arrs, datacube.date_to_int(date(year + 1, 1, 1), EPOCH))
    for d, var, (row, col), value in writes:
        idx = datacube.ensure_time_index(time_arr, arrs, datacube.date_to_int(d, EPOCH))
        datacube.write_region(arrs[var], idx, _window(row, col), np.full((64, 64), value, dtype="uint8"))
    g.attrs["source_id"] = source
    g.attrs["last_updated"] = f"{year}-12-31T00:00:00+00:00"
    if events:
        g.attrs["atlantis_events"] = events
    datacube.consolidate(store)


def _merged(dest, source="modis"):
    return zarr.open_group(Path(dest) / "datacube.zarr", mode="r")[source]


def _value_at(group, d, var, row, col):
    dates = datacube.decode_axis_dates(group)
    return int(group[var][dates.index(d), row, col])


def _run(tmp_path, *extra):
    return merge.main(
        [
            "--src-root",
            str(tmp_path / "src"),
            "--years",
            "2019-2022",
            "--dest",
            str(tmp_path / "archive"),
            "--skip-stac",
            *extra,
        ]
    )


@pytest.fixture
def years(tmp_path):
    src = tmp_path / "src"
    writes = {
        2019: [
            (date(2019, 3, 1), "water_fraction", (0, 0), 10),
            (date(2019, 12, 31), "exclusion_mask", (2048, 4096), 1),
        ],
        2020: [(date(2020, 2, 29), "water_fraction", (100, 3000), 55)],
        # Data-proven axis written out of date order.
        2022: [(date(2022, 6, 2), "water_fraction", (0, 0), 7), (date(2022, 6, 1), "water_fraction", (0, 0), 6)],
    }
    _make_year(
        src, 2019, writes[2019], events={"Ev": {"bbox": [0, 0, 1, 1], "dates": ["2019-03-01"], "updated_at": "a"}}
    )
    _make_year(src, 2020, writes[2020])
    _make_year(src, 2021, [(date(2021, 5, 5), "water_fraction", (4096, 0), 42)], stray=True)
    _make_year(src, 2022, writes[2022], prefill=False)
    return writes


def test_merge_preserves_data_on_contiguous_axis(tmp_path, years):
    assert _run(tmp_path, "--verify-samples", "5") == 0
    g = _merged(tmp_path / "archive")

    # Every day of 2019-2022; stray 2022-01-01 slot of 2021 dropped, 2022 gap days filled.
    assert datacube.decode_axis_dates(g) == [date(2019, 1, 1) + timedelta(days=i) for i in range(DAYS_2019_2022)]
    assert _value_at(g, date(2019, 3, 1), "water_fraction", 10, 10) == 10
    assert _value_at(g, date(2019, 12, 31), "exclusion_mask", 2050, 4100) == 1
    assert _value_at(g, date(2020, 2, 29), "water_fraction", 120, 3010) == 55
    assert _value_at(g, date(2021, 5, 5), "water_fraction", 4100, 5) == 42
    assert _value_at(g, date(2022, 6, 1), "water_fraction", 0, 0) == 6
    assert _value_at(g, date(2022, 6, 2), "water_fraction", 0, 0) == 7
    assert _value_at(g, date(2020, 7, 1), "water_fraction", 0, 0) == datacube.NODATA

    assert "recurring_flood" not in g  # no data in any year
    assert g["water_fraction"].chunks == (1, 256, 256)
    assert g["water_fraction"].shards == (1, 2048, 2048)
    attrs = dict(g.attrs)
    assert "atlantis_time_prefill" not in attrs
    assert attrs["atlantis_merged_years"] == [2019, 2020, 2021, 2022]
    assert attrs["atlantis_events"]["Ev"]["dates"] == ["2019-03-01"]
    assert attrs["archive_config"]["time_units"] == UNITS
    assert attrs["last_updated"] == "2022-12-31T00:00:00+00:00"


def test_sparse_axis_keeps_only_yearly_days(tmp_path, years):
    assert _run(tmp_path, "--sparse") == 0
    g = _merged(tmp_path / "archive")
    expected = [date(2019, 1, 1) + timedelta(days=i) for i in range(365 + 366 + 365)]
    assert datacube.decode_axis_dates(g) == [*expected, date(2022, 6, 1), date(2022, 6, 2)]
    assert _value_at(g, date(2022, 6, 1), "water_fraction", 0, 0) == 6
    assert _value_at(g, date(2022, 6, 2), "water_fraction", 0, 0) == 7


def test_gap_day_backfills_into_existing_slot(tmp_path, years):
    _run(tmp_path)
    g = zarr.open_group(tmp_path / "archive/datacube.zarr", path="modis", mode="a", use_consolidated=False)
    time_arr, arrs = datacube.get_handles(g, ["water_fraction"])
    gap_day = date(2022, 3, 15)  # in no yearly store
    idx = datacube.ensure_time_index(time_arr, arrs, datacube.date_to_int(gap_day, EPOCH))
    datacube.write_region(arrs["water_fraction"], idx, _window(0, 0), np.full((64, 64), 33, dtype="uint8"))

    assert time_arr.shape == (DAYS_2019_2022,)  # no append: the slot pre-existed
    assert datacube.decode_axis_dates(g)[idx] == gap_day
    assert int(g["water_fraction"][idx, 5, 5]) == 33


def test_merged_metadata_matches_yearly_except_shape(tmp_path, years):
    _run(tmp_path)
    src = json.loads((tmp_path / "src/2020/datacube.zarr/modis/water_fraction/zarr.json").read_text())
    dst = json.loads((tmp_path / "archive/datacube.zarr/modis/water_fraction/zarr.json").read_text())
    assert {k: v for k, v in src.items() if k != "shape"} == {k: v for k, v in dst.items() if k != "shape"}
    assert dst["shape"] == [DAYS_2019_2022, 10800, 21600]


def test_rerun_copies_nothing_and_picks_up_changes(tmp_path, years):
    _run(tmp_path)
    fs = merge._filesystem(str(tmp_path / "archive"), None)
    groups = merge.load_year_groups(fs, str(tmp_path / "src"), [2019, 2020, 2021, 2022], None, None)
    plan = merge.plan_source("modis", groups["modis"], (date(2019, 1, 1), date(2022, 12, 31)))
    dest_group = merge._fs_path(str(tmp_path / "archive"), "datacube.zarr", "modis")
    assert merge.plan_copies(fs, plan, dest_group).pairs == []

    # A later write into a yearly store (e.g. a backfill) is picked up by a re-run.
    store = tmp_path / "src/2020/datacube.zarr"
    g = zarr.open_group(store, mode="a", use_consolidated=False)["modis"]
    idx = datacube.decode_axis_dates(g).index(date(2020, 2, 29))
    datacube.write_region(g["water_fraction"], idx, _window(100, 3000, 128), np.full((128, 128), 77, dtype="uint8"))
    assert _run(tmp_path) == 0
    assert _value_at(_merged(tmp_path / "archive"), date(2020, 2, 29), "water_fraction", 200, 3100) == 77


def test_populated_out_of_year_slot_fails(tmp_path):
    src = tmp_path / "src"
    _make_year(src, 2021, [(date(2022, 1, 1), "water_fraction", (0, 0), 1)], stray=True)
    with pytest.raises(merge.MergeError, match="outside the year"):
        _run(tmp_path)
    assert not (tmp_path / "archive").exists()


def test_layout_mismatch_fails(tmp_path):
    src = tmp_path / "src"
    _make_year(src, 2019, [(date(2019, 1, 1), "water_fraction", (0, 0), 1)])
    _make_year(src, 2020, [(date(2020, 1, 1), "water_fraction", (0, 0), 1)], chunk=512)
    with pytest.raises(merge.MergeError, match="differs"):
        _run(tmp_path)


def test_changed_axis_requires_rebuild(tmp_path):
    src = tmp_path / "src"
    _make_year(src, 2022, [(date(2022, 6, 1), "water_fraction", (0, 0), 1)], prefill=False)
    _run(tmp_path, "--sparse")
    # An earlier date would shift the existing slots.
    _make_year(src, 2022, [(date(2022, 5, 1), "water_fraction", (0, 0), 5)], prefill=False)
    with pytest.raises(merge.MergeError, match="--rebuild-changed"):
        _run(tmp_path, "--sparse")
    assert _run(tmp_path, "--sparse", "--rebuild-changed") == 0
    g = _merged(tmp_path / "archive")
    assert datacube.decode_axis_dates(g) == [date(2022, 5, 1), date(2022, 6, 1)]
    assert _value_at(g, date(2022, 5, 1), "water_fraction", 0, 0) == 5
    assert _value_at(g, date(2022, 6, 1), "water_fraction", 0, 0) == 1


def test_sparse_tail_append_extends_in_place(tmp_path):
    src = tmp_path / "src"
    _make_year(src, 2022, [(date(2022, 6, 1), "water_fraction", (0, 0), 1)], prefill=False)
    _run(tmp_path, "--sparse")
    _make_year(src, 2022, [(date(2022, 6, 3), "water_fraction", (0, 0), 3)], prefill=False)
    assert _run(tmp_path, "--sparse") == 0
    g = _merged(tmp_path / "archive")
    assert datacube.decode_axis_dates(g) == [date(2022, 6, 1), date(2022, 6, 3)]
    assert _value_at(g, date(2022, 6, 3), "water_fraction", 0, 0) == 3


def test_adding_a_year_appends_without_recopying(tmp_path, years):
    dest = tmp_path / "archive"
    base = ["--src-root", str(tmp_path / "src"), "--dest", str(dest), "--skip-stac"]
    assert merge.main([*base, "--years", "2019-2021"]) == 0
    shard = dest / "datacube.zarr/modis/water_fraction/c/59/0/0"  # 2019-03-01
    mtime = shard.stat().st_mtime_ns

    assert merge.main([*base, "--years", "2019-2022"]) == 0
    g = _merged(dest)
    assert len(datacube.decode_axis_dates(g)) == DAYS_2019_2022
    assert shard.stat().st_mtime_ns == mtime  # existing shard not re-copied
    assert _value_at(g, date(2019, 3, 1), "water_fraction", 10, 10) == 10
    assert _value_at(g, date(2022, 6, 2), "water_fraction", 0, 0) == 7
    assert g.attrs["atlantis_merged_years"] == [2019, 2020, 2021, 2022]


def test_dest_must_not_be_a_year_root(tmp_path):
    with pytest.raises(merge.MergeError, match="yearly root"):
        merge.main(["--src-root", str(tmp_path), "--dest", str(tmp_path / "zarr" / "2020"), "--skip-stac"])


def test_stac_indexes_merged_axis(tmp_path):
    src = tmp_path / "src"
    _make_year(src, 2021, [(date(2021, 5, 5), "water_fraction", (0, 0), 1)], prefill=False)
    _make_year(src, 2022, [(date(2022, 6, 1), "water_fraction", (0, 0), 1)], prefill=False)
    dest = tmp_path / "archive"
    assert merge.main(["--src-root", str(src), "--years", "2021-2022", "--dest", str(dest), "--no-compute-bbox"]) == 0

    import pystac

    cat = pystac.Catalog.from_file(str(dest / "stac" / "catalog.json"))
    ids = {item.id for item in cat.get_items(recursive=True)}
    assert len(ids) == 365 + 365  # one item per day of the contiguous axis
    assert {"modis-2021-05-05", "modis-2022-06-01", "modis-2021-01-01", "modis-2022-12-31"} <= ids
