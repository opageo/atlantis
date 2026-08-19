"""Tests for GEOID-Flood metadata helpers."""

import pandas as pd
import pytest
import shapely.geometry
import shapely.wkb

from atlantis.utils.geoidflood import (
    GEOIDFLOOD_REQUIRED_COLUMNS,
    derive_geoidflood_metadata,
    load_geoidflood_catalog,
    load_geoidflood_metadata,
    write_geoidflood_metadata_csv,
)


def _wkb(west, south, east, north) -> bytes:
    return shapely.wkb.dumps(shapely.geometry.box(west, south, east, north))


def _sample_catalog() -> pd.DataFrame:
    """Synthetic tile catalogue in EPSG:4326 for exact bbox assertions.

    Rows:
    * EMSR900 AoI 1: two valid tiles, window 2020-01-10 → 2020-03-15.
    * EMSR901 AoI 2: one valid tile; one corrupt row (post 2041) and one with
      post < pre that must both be dropped by derive.
    """
    return pd.DataFrame(
        [
            {
                "event_id": "EMSR900",
                "tile_id": "1",
                "bbox_id": "0",
                "geometry": _wkb(-5.0, -2.0, 0.0, 2.0),
                "utm_crs": "EPSG:4326",
                "delineation_time_pre": "2020-01-10T06:00:00",
                "delineation_time_post": "2020-03-15T18:00:00",
                "event_time": "2020-02-01T12:00:00",
                "is_valid": True,
                "invalid_pixel_frac": 0.1,
                "split": "train",
                "countries": "Italy",
                "macro_areas_set": "[Europe]",
            },
            {
                "event_id": "EMSR900",
                "tile_id": "1",
                "bbox_id": "1",
                "geometry": _wkb(0.0, -2.0, 5.0, 2.0),
                "utm_crs": "EPSG:4326",
                "delineation_time_pre": "2020-01-10T06:00:00",
                "delineation_time_post": "2020-03-15T18:00:00",
                "event_time": "2020-02-01T12:00:00",
                "is_valid": True,
                "invalid_pixel_frac": 0.2,
                "split": "train",
                "countries": "Italy",
                "macro_areas_set": "[Europe]",
            },
            {
                "event_id": "EMSR901",
                "tile_id": "2",
                "bbox_id": "0",
                "geometry": _wkb(40.0, -5.0, 41.0, -4.0),
                "utm_crs": "EPSG:4326",
                "delineation_time_pre": "2021-06-01T06:00:00",
                "delineation_time_post": "2021-06-20T18:00:00",
                "event_time": "2021-06-10T12:00:00",
                "is_valid": False,
                "invalid_pixel_frac": 0.9,
                "split": "test",
                "countries": "Somalia",
                "macro_areas_set": "[Africa]",
            },
            {
                "event_id": "EMSR901",
                "tile_id": "2",
                "bbox_id": "1",
                "geometry": _wkb(41.0, -5.0, 42.0, -4.0),
                "utm_crs": "EPSG:4326",
                "delineation_time_pre": "2021-06-01T06:00:00",
                "delineation_time_post": "2041-06-20T18:00:00",  # corrupt → dropped
                "event_time": "2021-06-10T12:00:00",
                "is_valid": True,
                "invalid_pixel_frac": 0.0,
                "split": "test",
                "countries": "Somalia",
                "macro_areas_set": "[Africa]",
            },
            {
                "event_id": "EMSR901",
                "tile_id": "2",
                "bbox_id": "2",
                "geometry": _wkb(42.0, -5.0, 43.0, -4.0),
                "utm_crs": "EPSG:4326",
                "delineation_time_pre": "2021-06-30T06:00:00",  # post < pre → dropped
                "delineation_time_post": "2021-06-20T18:00:00",
                "event_time": "2021-06-10T12:00:00",
                "is_valid": True,
                "invalid_pixel_frac": 0.0,
                "split": "test",
                "countries": "Somalia",
                "macro_areas_set": "[Africa]",
            },
        ]
    )


def _catalog_file(tmp_path) -> pd.DataFrame:
    path = tmp_path / "tile_catalog.parquet"
    _sample_catalog().to_parquet(path, index=False)
    return path


def test_load_geoidflood_catalog_decodes_geometry(tmp_path):
    path = _catalog_file(tmp_path)
    catalog = load_geoidflood_catalog(path)

    assert len(catalog) == 5
    assert catalog["geometry"].iloc[0].geom_type == "Polygon"
    assert catalog["geometry"].iloc[0].bounds == (-5.0, -2.0, 0.0, 2.0)


def test_load_geoidflood_catalog_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="tile catalogue not found"):
        load_geoidflood_catalog(tmp_path / "missing.parquet")


def test_derive_geoidflood_metadata(tmp_path):
    path = _catalog_file(tmp_path)
    metadata = derive_geoidflood_metadata(path)

    assert list(metadata["event_id"]) == ["EMSR900", "EMSR901"]
    assert list(metadata["aoi_id"]) == ["1", "2"]
    # corrupt rows dropped: 3 input tiles for EMSR901 → 1 remaining
    assert list(metadata["n_tiles"]) == [2, 1]
    assert list(metadata["split"]) == ["train", "test"]
    assert bool(metadata.loc[0, "is_valid"]) is True
    assert bool(metadata.loc[1, "is_valid"]) is False

    row = metadata.loc[0]
    assert row["date_start"] == "2020-01-10"
    assert row["date_end"] == "2020-03-15"
    assert row["date_of_event"] == "2020-02-01"
    assert row["date_of_max_flood_extent"] == "2020-03-15"
    assert row["lon_min"] == -5.0
    assert row["lon_max"] == 5.0
    assert row["lat_min"] == -2.0
    assert row["lat_max"] == 2.0


def test_derive_geoidflood_metadata_geometry_is_4326_regardless_of_utm_crs(tmp_path):
    """Tile geometry is stored in degrees; a UTM-labeled tile must pass through unchanged."""
    catalog = _sample_catalog()
    catalog.loc[0, "utm_crs"] = "EPSG:32629"  # raster grid CRS — geometry stays degrees
    path = tmp_path / "tile_catalog.parquet"
    catalog.to_parquet(path, index=False)

    metadata = derive_geoidflood_metadata(path)
    row = metadata.loc[0]
    assert row["lon_min"] == -5.0
    assert row["lon_max"] == 5.0
    assert row["lat_min"] == -2.0
    assert row["lat_max"] == 2.0


def test_write_geoidflood_metadata_csv(tmp_path):
    path = _catalog_file(tmp_path)
    output_path = tmp_path / "metadata.csv"
    written = write_geoidflood_metadata_csv(path, output_path)

    assert written == output_path
    reloaded = pd.read_csv(output_path)
    assert list(reloaded["event_id"]) == ["EMSR900", "EMSR901"]


def test_load_geoidflood_metadata_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="metadata CSV not found"):
        load_geoidflood_metadata(tmp_path / "missing.csv")


def test_load_geoidflood_metadata_missing_columns(tmp_path):
    csv = tmp_path / "partial.csv"
    csv.write_text("event_id,aoi_id,date_start,date_end\nEMSR900,1,2020-01-10,2020-03-15\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required columns"):
        load_geoidflood_metadata(csv)


def test_required_columns_cover_aoi_table_columns():
    assert {"event_id", "aoi_id", "date_start", "date_end", "date_of_event"} <= GEOIDFLOOD_REQUIRED_COLUMNS
