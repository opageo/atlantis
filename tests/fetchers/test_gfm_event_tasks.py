"""Tests for the shared per-tile GFM event task building."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from atlantis.fetchers.gfm.event_tasks import (
    build_tasks_from_catalogues,
    build_tasks_live,
    is_valid_bbox,
    is_valid_tile,
    kurosiwo_task_id,
    task_id,
)

CATALOGUE = pd.DataFrame(
    [
        {
            "date": "2021-08-01",
            "equi7_tile": "AF020M_E030N066T3",
            "item_id": "i1",
            "item_href": "s3://x/1.json",
            "west": 14.7,
            "south": 12.0,
            "east": 15.2,
            "north": 12.5,
        },
        {
            "date": "2021-08-01",
            "equi7_tile": "AF020M_E030N066T3",
            "item_id": "i2",
            "item_href": "s3://x/2.json",
            "west": 14.8,
            "south": 12.1,
            "east": 15.3,
            "north": 12.6,
        },
        {
            "date": "2021-08-02",
            "equi7_tile": "AF020M_E030N066T3",
            "item_id": "i4",
            "item_href": "s3://x/4.json",
            "west": 14.7,
            "south": 12.0,
            "east": 15.2,
            "north": 12.5,
        },
    ]
)

AOI_TABLE = pd.DataFrame(
    [
        {
            "event_id": "KuroSiwo_470",
            "aoi_id": "KuroSiwo_470",
            "aoi_west": 14.79,
            "aoi_south": 12.17,
            "aoi_east": 14.92,
            "aoi_north": 12.32,
            "date_start": "2021-07-01",
            "date_end": "2021-08-05",
        },
        {
            "event_id": "KuroSiwo_999",
            "aoi_id": "KuroSiwo_999",
            "aoi_west": -80.0,
            "aoi_south": 10.0,
            "aoi_east": -79.0,
            "aoi_north": 11.0,
            "date_start": "2020-08-01",
            "date_end": "2020-08-02",
        },
    ]
)


class TestTileValidation:
    def test_valid_tile(self):
        assert is_valid_tile("AF020M_E030N066T3")
        assert is_valid_tile("EU020M_E036N009T3")

    @pytest.mark.parametrize(
        "value",
        ["", "a512_r09c020", "AF020M-E030N066T3", "af020m_e030n066t3", "AF020M E030", 123, None, ["x"]],
    )
    def test_invalid_tile(self, value):
        assert not is_valid_tile(value)

    def test_valid_bbox(self):
        assert is_valid_bbox([14.7, 12.0, 15.2, 12.5])

    @pytest.mark.parametrize(
        "bbox",
        [
            None,
            [14.7, 12.0, 15.2],
            [15.2, 12.0, 14.7, 12.5],
            [14.7, 12.0, 15.2, float("nan")],
            [14.7, 12.0, 15.2, 12.5, 0.0],
            [float("inf"), 12.0, 15.2, 12.5],
            [14.7, 12.0, 15.2, 95.0],
        ],
    )
    def test_invalid_bbox(self, bbox):
        assert not is_valid_bbox(bbox)


class TestTaskId:
    def test_geoidflood_embeds_event_and_aoi(self):
        assert task_id("EMSR712", "10", "2024-10-29", "EU020M_E036N009T3") == (
            "gfm-EMSR712-10-EU020M_E036N009T3-20241029"
        )

    def test_kurosiwo_embeds_tile_so_same_day_does_not_collide(self):
        a = kurosiwo_task_id("KuroSiwo_470", "KuroSiwo_470", "2024-10-29", "AF020M_E030N066T3")
        b = kurosiwo_task_id("KuroSiwo_470", "KuroSiwo_470", "2024-10-29", "AF020M_E030N067T3")
        assert a != b


class TestBuildTasksFromCatalogues:
    def test_one_task_per_tile_day_with_tile_bbox(self, monkeypatch):
        import atlantis.fetchers.gfm.inventory as inventory_mod

        monkeypatch.setattr(inventory_mod, "load_inventory", lambda uri: CATALOGUE)

        tasks, live_events = build_tasks_from_catalogues(AOI_TABLE, kurosiwo_task_id)

        assert live_events == {"KuroSiwo_999"}
        assert len(tasks) == 2
        for task in tasks:
            assert task["equi7_tile"] == "AF020M_E030N066T3"
            assert task["date"] in ("2021-08-01", "2021-08-02")
            assert task["bbox"] == [14.7, 12.0, 15.3, 12.6] or task["bbox"] == [14.7, 12.0, 15.2, 12.5]
            assert task["task_id"].endswith(f"-{task['equi7_tile']}-{task['date'].replace('-', '')}")
        assert len({t["task_id"] for t in tasks}) == 2


def _fake_item(item_id, tile, bbox, href="s3://x/item.json"):
    return SimpleNamespace(id=item_id, self_href=href, properties={"Equi7Tile": tile}, bbox=bbox)


class TestBuildTasksLive:
    def test_groups_by_tile_and_reports_dropped(self, monkeypatch):
        from atlantis.fetchers.gfm import event_tasks as et

        day_items = [
            _fake_item("ok1", "AF020M_E030N066T3", [14.7, 12.0, 15.2, 12.5]),
            _fake_item("ok2", "AF020M_E030N066T3", [14.8, 12.1, 15.3, 12.6]),
            _fake_item("no-tile", None, [14.7, 12.0, 15.2, 12.5]),
            _fake_item("bad-bbox", "AF020M_E030N066T3", [15.2, 12.0, 14.7, 12.5]),
        ]
        calls = []

        class FakeBackend:
            def search(self, event):
                calls.append(event)
                return day_items

        monkeypatch.setattr(et, "GfmStacBackend", FakeBackend)

        table = AOI_TABLE[AOI_TABLE["event_id"] == "KuroSiwo_999"]
        tasks, dropped = build_tasks_live(table, {"KuroSiwo_999"}, kurosiwo_task_id)

        assert len(calls) == 2  # one per day of the window
        assert len(tasks) == 2  # one per day
        for task in tasks:
            assert task["equi7_tile"] == "AF020M_E030N066T3"
            assert task["item_hrefs"] == ["s3://x/item.json", "s3://x/item.json"]
            assert task["bbox"] == [14.7, 12.0, 15.3, 12.6]
        assert [d["item_id"] for d in dropped] == ["no-tile", "bad-bbox", "no-tile", "bad-bbox"]
        assert "reason" in dropped[0]
