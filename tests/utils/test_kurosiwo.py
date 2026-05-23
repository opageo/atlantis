"""Tests for KuroSiwo metadata helpers."""

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from atlantis.utils.kurosiwo import (
    build_kurosiwo_flood_events,
    build_kurosiwo_flood_events_from_catalogue,
    derive_kurosiwo_metadata,
    load_kurosiwo_metadata,
    write_kurosiwo_metadata_csv,
)


def _sample_catalogue() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "actid": [470, 470],
            "master": [False, True],
            "crank": [1, 1],
            "source_date": [pd.Timestamp("2020-04-29"), pd.Timestamp("2020-10-14")],
            "pflood": [0.0, 50.0],
        },
        geometry=[box(-0.8627, 8.2639, 1.9947, 11.7312), box(-0.8627, 8.2639, 1.9947, 11.7312)],
        crs="EPSG:4326",
    )


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


def test_derive_kurosiwo_metadata(monkeypatch, tmp_path):
    catalogue_path = tmp_path / "catalogue.gpkg"
    catalogue_path.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr("atlantis.utils.kurosiwo.gpd.read_file", lambda _path: _sample_catalogue())

    metadata = derive_kurosiwo_metadata(catalogue_path)

    assert list(metadata["flood_case"]) == ["KuroSiwo_470"]
    assert metadata.loc[0, "date_start"].isoformat() == "2020-04-29"
    assert metadata.loc[0, "date_end"].isoformat() == "2020-10-14"
    assert metadata.loc[0, "lon_min"] == -0.8627
    assert metadata.loc[0, "lat_max"] == 11.7312
    assert metadata.loc[0, "max_flood_extent_km2"] > 0


def test_write_kurosiwo_metadata_csv(monkeypatch, tmp_path):
    catalogue_path = tmp_path / "catalogue.gpkg"
    catalogue_path.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr("atlantis.utils.kurosiwo.gpd.read_file", lambda _path: _sample_catalogue())

    output_path = tmp_path / "metadata.csv"
    written = write_kurosiwo_metadata_csv(catalogue_path, output_path)

    assert written == output_path
    reloaded = pd.read_csv(output_path)
    assert list(reloaded["flood_case"]) == ["KuroSiwo_470"]


def test_build_kurosiwo_flood_events_from_catalogue(monkeypatch, tmp_path):
    catalogue_path = tmp_path / "catalogue.gpkg"
    catalogue_path.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr("atlantis.utils.kurosiwo.gpd.read_file", lambda _path: _sample_catalogue())

    events = build_kurosiwo_flood_events_from_catalogue(catalogue_path, case="KuroSiwo_470")

    assert len(events) == 1
    assert events[0].event_id == "KuroSiwo_470"
    assert events[0].start_date.isoformat() == "2020-10-14"
