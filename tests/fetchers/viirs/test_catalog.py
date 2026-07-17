"""Unit tests for viirs/catalog.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from atlantis.fetchers.viirs.backend import ListingLocation
from atlantis.fetchers.viirs.catalog import _parse_aoi_id, build_catalog


def test_parse_aoi_id_matches():
    key = "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/01/01/VIIRS-Flood-1day-GLB003_v1r0_blend_s1_e1_c1.tif"
    assert _parse_aoi_id(key) == 3


def test_parse_aoi_id_no_match():
    assert _parse_aoi_id("some/other/file.txt") is None


@pytest.fixture()
def fake_aoi_grid(tmp_path, monkeypatch):
    """Write a minimal 2-tile AOI grid and point the module at it."""
    gdf = gpd.GeoDataFrame(
        {"AOI_ID": [3, 4]},
        geometry=[box(0, 0, 1, 1), box(1, 1, 2, 2)],
        crs="EPSG:4326",
    )
    path = tmp_path / "viirs_aois.geojson"
    gdf.to_file(path, driver="GeoJSON")
    monkeypatch.setattr("atlantis.fetchers.viirs.catalog._AOI_GRID_PATH", path)
    return path


def _entries_for(date_str: str) -> list[str]:
    aoi = {"2024-01-01": 3, "2024-01-02": 4}[date_str]
    return [f"prefix/{date_str}/VIIRS-Flood-1day-GLB{aoi:03d}_v1r0_blend_s1_e1_c1.tif"]


def test_build_catalog_writes_expected_schema(fake_aoi_grid, tmp_path):
    with patch("atlantis.fetchers.viirs.catalog.NoaaS3Backend") as mock_backend_cls:
        mock_backend = MagicMock()
        mock_backend.get_listing_location.side_effect = lambda base_url, event_date, data_format: ListingLocation(
            locator=f"prefix/{event_date.date().isoformat()}/", date_token=event_date.strftime("%Y%m%d")
        )
        mock_backend.get_directory_links.side_effect = lambda base_url, location, timeout: _entries_for(
            location.split("/")[1]
        )
        mock_backend_cls.return_value = mock_backend

        output = tmp_path / "out.parquet"
        result = build_catalog("2024-01-01", "2024-01-02", output)

    assert result == output
    df = pd.read_parquet(output)
    assert set(df.columns) >= {"date", "aoi_id", "s3_key", "geometry"}
    assert len(df) == 2
    assert set(df["aoi_id"]) == {3, 4}


def test_build_catalog_raises_when_no_tiles(fake_aoi_grid, tmp_path):
    with patch("atlantis.fetchers.viirs.catalog.NoaaS3Backend") as mock_backend_cls:
        mock_backend = MagicMock()
        mock_backend.get_listing_location.return_value = ListingLocation(locator="prefix/", date_token="x")
        mock_backend.get_directory_links.return_value = []
        mock_backend_cls.return_value = mock_backend

        with pytest.raises(RuntimeError, match="No VIIRS tiles found"):
            build_catalog("2024-01-01", "2024-01-01", tmp_path / "out.parquet")


def test_build_catalog_missing_aoi_grid_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("atlantis.fetchers.viirs.catalog._AOI_GRID_PATH", tmp_path / "missing.geojson")
    with pytest.raises(FileNotFoundError):
        build_catalog("2024-01-01", "2024-01-01", tmp_path / "out.parquet")
