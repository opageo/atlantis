"""Tests for KuroSiwo metadata helpers."""

import pytest

from atlantis.utils.kurosiwo import build_kurosiwo_flood_events, load_kurosiwo_metadata


def test_load_kurosiwo_metadata(tmp_path):
    metadata_path = tmp_path / "kurosiwo.csv"
    metadata_path.write_text(
        "flood_case,date_start,date_end,lat_min,lat_max,lon_min,lon_max\n"
        "KuroSiwo_470,2020-04-29,2020-10-14,8.2639,11.7312,-0.8627,1.9947\n",
        encoding="utf-8",
    )

    dataframe = load_kurosiwo_metadata(metadata_path)

    assert list(dataframe["flood_case"]) == ["KuroSiwo_470"]
    assert dataframe.loc[0, "date_end"].date().isoformat() == "2020-10-14"


def test_build_kurosiwo_flood_events_defaults_to_date_end(tmp_path):
    metadata_path = tmp_path / "kurosiwo.csv"
    metadata_path.write_text(
        "flood_case,date_start,date_end,lat_min,lat_max,lon_min,lon_max\n"
        "KuroSiwo_470,2020-04-29,2020-10-14,8.2639,11.7312,-0.8627,1.9947\n",
        encoding="utf-8",
    )

    events = build_kurosiwo_flood_events(metadata_path, case="KuroSiwo_470")

    assert len(events) == 1
    assert events[0].event_id == "KuroSiwo_470"
    assert events[0].bbox == (-0.8627, 8.2639, 1.9947, 11.7312)
    assert events[0].start_date.isoformat() == "2020-10-14"
    assert events[0].end_date.isoformat() == "2020-10-14"


def test_build_kurosiwo_flood_events_can_use_metadata_range(tmp_path):
    metadata_path = tmp_path / "kurosiwo.csv"
    metadata_path.write_text(
        "flood_case,date_start,date_end,lat_min,lat_max,lon_min,lon_max\n"
        "KuroSiwo_470,2020-04-29,2020-10-14,8.2639,11.7312,-0.8627,1.9947\n",
        encoding="utf-8",
    )

    events = build_kurosiwo_flood_events(metadata_path, use_metadata_range=True)

    assert events[0].start_date.isoformat() == "2020-04-29"
    assert events[0].end_date.isoformat() == "2020-10-14"


def test_build_kurosiwo_flood_events_raises_for_missing_case(tmp_path):
    metadata_path = tmp_path / "kurosiwo.csv"
    metadata_path.write_text(
        "flood_case,date_start,date_end,lat_min,lat_max,lon_min,lon_max\n"
        "KuroSiwo_470,2020-04-29,2020-10-14,8.2639,11.7312,-0.8627,1.9947\n",
        encoding="utf-8",
    )

    with pytest.raises(KeyError, match="KuroSiwo flood case not found"):
        build_kurosiwo_flood_events(metadata_path, case="KuroSiwo_999")
