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


def test_is_lfs_pointer_detects_pointer(tmp_path):
    from atlantis.utils.kurosiwo import is_lfs_pointer

    f = tmp_path / "pointer.gpkg"
    f.write_text("version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 123\n", encoding="utf-8")
    assert is_lfs_pointer(f) is True


def test_is_lfs_pointer_not_a_pointer(tmp_path):
    from atlantis.utils.kurosiwo import is_lfs_pointer

    f = tmp_path / "real.gpkg"
    f.write_bytes(b"\x89PNG\x0d\x0a\x1a\x0a")
    assert is_lfs_pointer(f) is False


def test_is_lfs_pointer_missing_file(tmp_path):
    from atlantis.utils.kurosiwo import is_lfs_pointer

    assert is_lfs_pointer(tmp_path / "missing.gpkg") is False


def test_load_kurosiwo_catalogue_missing_file():
    from pathlib import Path

    import pytest

    from atlantis.utils.kurosiwo import load_kurosiwo_catalogue

    with pytest.raises(FileNotFoundError, match="KuroSiwo catalogue not found"):
        load_kurosiwo_catalogue(Path("/nonexistent/path.gpkg"))


def test_load_kurosiwo_catalogue_lfs_pointer(tmp_path):
    from atlantis.utils.kurosiwo import load_kurosiwo_catalogue

    f = tmp_path / "pointer.gpkg"
    f.write_text("version https://git-lfs.github.com/spec/v1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Git LFS pointer"):
        load_kurosiwo_catalogue(f)


def test_load_kurosiwo_metadata_missing_file():
    from pathlib import Path

    from atlantis.utils.kurosiwo import load_kurosiwo_metadata

    with pytest.raises(FileNotFoundError, match="KuroSiwo metadata CSV not found"):
        load_kurosiwo_metadata(Path("/nonexistent/metadata.csv"))


def test_load_kurosiwo_metadata_missing_columns(tmp_path):
    from atlantis.utils.kurosiwo import load_kurosiwo_metadata

    csv = tmp_path / "partial.csv"
    csv.write_text(
        "flood_case,date_start,date_end\nKuroSiwo_001,2020-01-01,2020-01-02\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing required columns"):
        load_kurosiwo_metadata(csv)


def test_build_kurosiwo_flood_events_invalid_limit(monkeypatch, tmp_path):
    from atlantis.utils.kurosiwo import build_kurosiwo_flood_events_from_dataframe

    metadata_path = tmp_path / "kurosiwo.csv"
    metadata_path.write_text(
        "flood_case,date_start,date_end,lat_min,lat_max,lon_min,lon_max\n"
        "KuroSiwo_470,2020-04-29,2020-10-14,8.2639,11.7312,-0.8627,1.9947\n",
        encoding="utf-8",
    )

    from atlantis.utils.kurosiwo import load_kurosiwo_metadata

    df = load_kurosiwo_metadata(metadata_path)
    with pytest.raises(ValueError, match="limit must be"):
        build_kurosiwo_flood_events_from_dataframe(df, limit=0)

    with pytest.raises(ValueError, match="limit must be"):
        build_kurosiwo_flood_events_from_dataframe(df, limit=-1)
