"""Tests for the VIIRS fetcher."""

from datetime import date, datetime, timezone
from pathlib import Path
from shutil import copyfile
from zipfile import ZipFile

import numpy as np
import rasterio
from rasterio.transform import from_origin

from atlantis.fetchers.base import SearchResult
from atlantis.fetchers.viirs import VIIRSFetcher
from atlantis.models.event import FloodEvent


def _write_tile(path: Path, west: float, south: float, east: float, north: float, data: np.ndarray) -> None:
    height, width = data.shape
    transform = from_origin(west, north, (east - west) / width, (north - south) / height)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(data, 1)


def _zip_file(zip_path: Path, file_path: Path) -> None:
    with ZipFile(zip_path, "w") as archive:
        archive.write(file_path, arcname=file_path.name)


def test_search_returns_intersecting_aoi_results(monkeypatch):
    fetcher = VIIRSFetcher()
    event = FloodEvent(
        event_id="Yangtze_2020",
        bbox=(105.0, 28.0, 125.0, 38.0),
        start_date=date(2020, 7, 22),
        end_date=date(2020, 7, 22),
        sources=["viirs"],
    )

    hrefs = [
        "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB077_v1r0_blend_s202007220000000_e202007222359590_c202205240401305.tif",
        "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB078_v1r0_blend_s202007220000000_e202007222359590_c202205240401363.tif",
        "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB079_v1r0_blend_s202007220000000_e202007222359590_c202205240401428.tif",
        "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB090_v1r0_blend_s202007220000000_e202007222359590_c202205240402464.tif",
        "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB091_v1r0_blend_s202007220000000_e202007222359590_c202205240402523.tif",
        "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB092_v1r0_blend_s202007220000000_e202007222359590_c202205240402593.tif",
    ]
    monkeypatch.setattr(fetcher.backend, "get_directory_links", lambda _base_url, _location, _timeout: hrefs)

    results = fetcher.search(event)

    assert len(results) == 6
    assert {result.properties["aoi_id"] for result in results} == {77, 78, 79, 90, 91, 92}
    assert all(result.properties["date"] == "20200722" for result in results)
    assert all(result.properties["backend"] == "noaa_s3" for result in results)


def test_search_supports_legacy_gmu_backend(monkeypatch):
    fetcher = VIIRSFetcher(backend="gmu_legacy")
    event = FloodEvent(
        event_id="Yangtze_2020",
        bbox=(105.0, 28.0, 125.0, 38.0),
        start_date=date(2020, 7, 22),
        end_date=date(2020, 7, 22),
        sources=["viirs"],
    )

    hrefs = [
        "WATER_COM_VIIRS_Prj_SVI_d20200718_d20200722_4448_4448_35_005day_077.tif.zip",
        "WATER_COM_VIIRS_Prj_SVI_d20200718_d20200722_4448_4448_34_005day_078.tif.zip",
    ]
    monkeypatch.setattr(fetcher.backend, "get_directory_links", lambda _base_url, _location, _timeout: hrefs)

    results = fetcher.search(event)

    assert len(results) == 2
    assert all(result.properties["backend"] == "gmu_legacy" for result in results)


def test_fetch_and_to_dataset(tmp_path, monkeypatch):
    fetcher = VIIRSFetcher()
    event = FloodEvent(
        event_id="Yangtze_2020",
        bbox=(105.0, 28.0, 125.0, 38.0),
        start_date=date(2020, 7, 22),
        end_date=date(2020, 7, 22),
        sources=["viirs"],
    )

    tile1_data = np.full((10, 10), 170, dtype=np.uint8)
    tile2_data = np.full((10, 10), 17, dtype=np.uint8)
    tile2_data[5:, :] = 30

    tile1_tif = tmp_path / "tile_077.tif"
    tile2_tif = tmp_path / "tile_078.tif"
    _write_tile(tile1_tif, 105.0, 28.0, 115.0, 38.0, tile1_data)
    _write_tile(tile2_tif, 115.0, 28.0, 125.0, 38.0, tile2_data)

    tile1_zip = tmp_path / "tile_077.tif.zip"
    tile2_zip = tmp_path / "tile_078.tif.zip"
    _zip_file(tile1_zip, tile1_tif)
    _zip_file(tile2_zip, tile2_tif)

    search_results = [
        SearchResult(
            source_id="viirs",
            item_id="viirs:20200722:077",
            timestamp=datetime(2020, 7, 22, tzinfo=timezone.utc),
            bbox=(105.0, 28.0, 115.0, 38.0),
            url="https://example.com/tile_077.tif.zip",
            properties={"aoi_id": 77, "date": "20200722", "filename": tile1_zip.name},
        ),
        SearchResult(
            source_id="viirs",
            item_id="viirs:20200722:078",
            timestamp=datetime(2020, 7, 22, tzinfo=timezone.utc),
            bbox=(115.0, 28.0, 125.0, 38.0),
            url="https://example.com/tile_078.tif.zip",
            properties={"aoi_id": 78, "date": "20200722", "filename": tile2_zip.name},
        ),
    ]

    monkeypatch.setattr(fetcher, "search", lambda _event: search_results)

    def fake_download(url: str, output_path: Path | None = None, **_kwargs) -> Path:
        source = tile1_zip if url.endswith("tile_077.tif.zip") else tile2_zip
        assert output_path is not None
        copyfile(source, output_path)
        return output_path

    monkeypatch.setattr("atlantis.fetchers.viirs.download_file", fake_download)

    results = fetcher.fetch(event, tmp_path / "out")

    assert len(results) == 1
    result = results[0]
    assert len(result.files) == 3
    assert result.metadata.event_id == event.event_id
    assert result.metadata.permanent_water_mask_available is True

    dataset = fetcher.to_dataset(result)

    assert set(dataset.data_vars) == {"flood_extent", "quality_mask", "permanent_water"}
    assert dataset["flood_extent"].dtype == np.float32
    assert dataset["quality_mask"].dtype == np.uint8
    assert dataset["permanent_water"].dtype == np.uint8
    assert float(dataset["flood_extent"].sum()) > 0
    assert int(dataset["quality_mask"].min()) == 0
    assert int(dataset["permanent_water"].max()) == 1
