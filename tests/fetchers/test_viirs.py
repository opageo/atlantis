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
    fetcher = VIIRSFetcher(backend="gmu_legacy", classify=True)
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
    fetcher = VIIRSFetcher(classify=True)
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
    # Cloud pixels (code 30) → quality=0; flood pixels (170) → quality=1
    assert int(dataset["quality_mask"].min()) == 0
    assert int(dataset["quality_mask"].max()) == 1
    assert int(dataset["permanent_water"].max()) == 1
    # Permanent water pixels (code 17) are valid observations → quality=1, not 0
    perm_water_mask = dataset["permanent_water"].values.astype(bool)
    assert (dataset["quality_mask"].values[perm_water_mask] == 1).all(), (
        "permanent water pixels should have quality=1 (valid observation)"
    )


def test_search_same_results_across_backends(tmp_path, monkeypatch):
    """Both VIIRS backends return equivalent results for the same event."""
    event = FloodEvent(
        event_id="Yangtze_2020",
        bbox=(105.0, 28.0, 125.0, 38.0),
        start_date=date(2020, 7, 22),
        end_date=date(2020, 7, 22),
        sources=["viirs"],
    )

    # ── Search-level equivalence ──────────────────────────────────────
    noaa_fetcher = VIIRSFetcher(backend="noaa_s3", classify=True)
    noaa_hrefs = [
        "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB077_v1r0_blend_s202007220000000_e202007222359590_c202205240401305.tif",
        "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB078_v1r0_blend_s202007220000000_e202007222359590_c202205240401363.tif",
        "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB079_v1r0_blend_s202007220000000_e202007222359590_c202205240401428.tif",
        "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB090_v1r0_blend_s202007220000000_e202007222359590_c202205240402464.tif",
        "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB091_v1r0_blend_s202007220000000_e202007222359590_c202205240402523.tif",
        "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB092_v1r0_blend_s202007220000000_e202007222359590_c202205240402593.tif",
    ]
    monkeypatch.setattr(noaa_fetcher.backend, "get_directory_links", lambda _base_url, _location, _timeout: noaa_hrefs)

    gmu_fetcher = VIIRSFetcher(backend="gmu_legacy", classify=True)
    gmu_hrefs = [
        "WATER_COM_VIIRS_Prj_SVI_d20200718_d20200722_4448_4448_35_005day_077.tif.zip",
        "WATER_COM_VIIRS_Prj_SVI_d20200718_d20200722_4448_4448_34_005day_078.tif.zip",
        "WATER_COM_VIIRS_Prj_SVI_d20200718_d20200722_4448_4448_33_005day_079.tif.zip",
        "WATER_COM_VIIRS_Prj_SVI_d20200718_d20200722_4448_4448_32_005day_090.tif.zip",
        "WATER_COM_VIIRS_Prj_SVI_d20200718_d20200722_4448_4448_31_005day_091.tif.zip",
        "WATER_COM_VIIRS_Prj_SVI_d20200718_d20200722_4448_4448_30_005day_092.tif.zip",
    ]
    monkeypatch.setattr(gmu_fetcher.backend, "get_directory_links", lambda _base_url, _location, _timeout: gmu_hrefs)

    noaa_results = noaa_fetcher.search(event)
    gmu_results = gmu_fetcher.search(event)

    # Search-level: both backends discover the same AOI IDs and dates
    assert len(noaa_results) == len(gmu_results)
    assert {r.properties["aoi_id"] for r in noaa_results} == {r.properties["aoi_id"] for r in gmu_results}
    assert {r.properties["date"] for r in noaa_results} == {r.properties["date"] for r in gmu_results}
    assert all(r.properties["backend"] == "noaa_s3" for r in noaa_results)
    assert all(r.properties["backend"] == "gmu_legacy" for r in gmu_results)

    # Search-level: every result respects the SearchResult data shape contract
    for results, expected_backend in [(noaa_results, "noaa_s3"), (gmu_results, "gmu_legacy")]:
        for result in results:
            assert result.source_id == "viirs"
            assert isinstance(result.item_id, str) and result.item_id.startswith("viirs:")
            assert isinstance(result.timestamp, datetime) and result.timestamp.tzinfo is not None
            assert isinstance(result.bbox, tuple) and len(result.bbox) == 4
            assert all(isinstance(v, float) for v in result.bbox)
            assert isinstance(result.url, str) and len(result.url) > 0
            assert isinstance(result.properties, dict)
            assert set(result.properties.keys()) == {"aoi_id", "date", "filename", "backend", "format"}
            assert isinstance(result.properties["aoi_id"], int)
            assert isinstance(result.properties["date"], str)
            assert isinstance(result.properties["filename"], str)
            assert result.properties["backend"] == expected_backend
            assert result.properties["format"] == "tif"

    # ── Raster-level equivalence ──────────────────────────────────────
    # Create synthetic tiles and run both backends through the full
    # search → download → materialise → process → dataset pipeline.

    # Tile 077: all flood
    tile1_data = np.full((10, 10), 170, dtype=np.uint8)
    # Tile 078: top half = permanent water, bottom half = cloud
    tile2_data = np.full((10, 10), 17, dtype=np.uint8)
    tile2_data[5:, :] = 30

    tile1_tif = tmp_path / "077.tif"
    tile2_tif = tmp_path / "078.tif"
    _write_tile(tile1_tif, 105.0, 28.0, 115.0, 38.0, tile1_data)
    _write_tile(tile2_tif, 115.0, 28.0, 125.0, 38.0, tile2_data)

    tile1_zip = tmp_path / "077.tif.zip"
    tile2_zip = tmp_path / "078.tif.zip"
    _zip_file(tile1_zip, tile1_tif)
    _zip_file(tile2_zip, tile2_tif)

    # Search results for NOAA backend (bare .tif)
    noaa_sr = [
        SearchResult(
            source_id="viirs",
            item_id="viirs:20200722:077",
            timestamp=datetime(2020, 7, 22, tzinfo=timezone.utc),
            bbox=(105.0, 28.0, 115.0, 38.0),
            url="http://noaa/077.tif",
            properties={"aoi_id": 77, "date": "20200722", "filename": "077.tif", "backend": "noaa_s3", "format": "tif"},
        ),
        SearchResult(
            source_id="viirs",
            item_id="viirs:20200722:078",
            timestamp=datetime(2020, 7, 22, tzinfo=timezone.utc),
            bbox=(115.0, 28.0, 125.0, 38.0),
            url="http://noaa/078.tif",
            properties={"aoi_id": 78, "date": "20200722", "filename": "078.tif", "backend": "noaa_s3", "format": "tif"},
        ),
    ]

    # Search results for GMU backend (.tif.zip — extracted later)
    gmu_sr = [
        SearchResult(
            source_id="viirs",
            item_id="viirs:20200722:077",
            timestamp=datetime(2020, 7, 22, tzinfo=timezone.utc),
            bbox=(105.0, 28.0, 115.0, 38.0),
            url="http://gmu/077.tif.zip",
            properties={
                "aoi_id": 77,
                "date": "20200722",
                "filename": "077.tif.zip",
                "backend": "gmu_legacy",
                "format": "tif",
            },
        ),
        SearchResult(
            source_id="viirs",
            item_id="viirs:20200722:078",
            timestamp=datetime(2020, 7, 22, tzinfo=timezone.utc),
            bbox=(115.0, 28.0, 125.0, 38.0),
            url="http://gmu/078.tif.zip",
            properties={
                "aoi_id": 78,
                "date": "20200722",
                "filename": "078.tif.zip",
                "backend": "gmu_legacy",
                "format": "tif",
            },
        ),
    ]

    # Override search on both fetchers to return local-mock results
    monkeypatch.setattr(noaa_fetcher, "search", lambda _event: noaa_sr)
    monkeypatch.setattr(gmu_fetcher, "search", lambda _event: gmu_sr)

    # Mock download: NOAA receives .tif files directly
    def noaa_download(url: str, output_path: Path | None = None, **_kwargs) -> Path:
        assert output_path is not None
        src = tile1_tif if "077" in url else tile2_tif
        copyfile(src, output_path)
        return output_path

    monkeypatch.setattr("atlantis.fetchers.viirs.download_file", noaa_download)
    noaa_fetch_results = noaa_fetcher.fetch(event, tmp_path / "noaa_out")

    # Mock download: GMU receives .tif.zip files
    def gmu_download(url: str, output_path: Path | None = None, **_kwargs) -> Path:
        assert output_path is not None
        src = tile1_zip if "077" in url else tile2_zip
        copyfile(src, output_path)
        return output_path

    monkeypatch.setattr("atlantis.fetchers.viirs.download_file", gmu_download)
    gmu_fetch_results = gmu_fetcher.fetch(event, tmp_path / "gmu_out")

    # Both backends produce exactly one merged result for this single date
    assert len(noaa_fetch_results) == len(gmu_fetch_results) == 1

    noaa_dataset = noaa_fetcher.to_dataset(noaa_fetch_results[0])
    gmu_dataset = gmu_fetcher.to_dataset(gmu_fetch_results[0])

    # Same data variables present
    assert set(noaa_dataset.data_vars) == {"flood_extent", "quality_mask", "permanent_water"}
    assert set(gmu_dataset.data_vars) == {"flood_extent", "quality_mask", "permanent_water"}

    # Same array shapes (mosaic of two 10×10 tiles → 10×20 for 1°×1° bbox)
    expected_shape = (10, 20)
    for var in ("flood_extent", "quality_mask", "permanent_water"):
        noaa_arr = noaa_dataset[var]
        gmu_arr = gmu_dataset[var]
        assert noaa_arr.shape == expected_shape, f"{var} shape mismatch (noaa)"
        assert gmu_arr.shape == expected_shape, f"{var} shape mismatch (gmu)"
        assert noaa_arr.shape == gmu_arr.shape, f"{var} shape differs between backends"

    # Same dtypes per variable (as documented in docs/viirs.md)
    assert noaa_dataset["flood_extent"].dtype == np.float32
    assert gmu_dataset["flood_extent"].dtype == np.float32
    assert noaa_dataset["quality_mask"].dtype == np.uint8
    assert gmu_dataset["quality_mask"].dtype == np.uint8
    assert noaa_dataset["permanent_water"].dtype == np.uint8
    assert gmu_dataset["permanent_water"].dtype == np.uint8

    # Same pixel values — the raster arrays are byte-identical
    for var in ("flood_extent", "quality_mask", "permanent_water"):
        assert (noaa_dataset[var].values == gmu_dataset[var].values).all(), f"{var} values differ between backends"

    # Semantic correctness: left half (077) is all flood, right half (078) is mixed
    # Columns 0-9 = tile 077 (flood = 170 → flood_extent = 1)
    assert float(noaa_dataset["flood_extent"].isel(x=slice(0, 10)).sum()) == 100.0, "left tile should be all flood"
    # Columns 10-19 tile 078 rows 0-4 = permanent water (17 → flood_extent = 0)
    assert float(noaa_dataset["flood_extent"].isel(x=slice(10, 20), y=slice(0, 5)).sum()) == 0.0
    # Columns 10-19 tile 078 rows 5-9 = cloud (30 → flood_extent = 0)
    assert float(noaa_dataset["flood_extent"].isel(x=slice(10, 20), y=slice(5, 10)).sum()) == 0.0

    # Quality mask: 1 = valid clear-sky observation, 0 = fill or cloud only.
    # Permanent water (17) is a valid sensor observation → quality=1.
    assert int(noaa_dataset["quality_mask"].isel(x=slice(0, 10)).sum()) == 100  # flood → valid
    assert int(noaa_dataset["quality_mask"].isel(x=slice(10, 20), y=slice(0, 5)).sum()) == 50  # perm water → valid
    assert int(noaa_dataset["quality_mask"].isel(x=slice(10, 20), y=slice(5, 10)).sum()) == 0  # cloud → invalid

    # Permanent water: right-half top rows only
    assert int(noaa_dataset["permanent_water"].isel(x=slice(0, 10)).sum()) == 0
    assert int(noaa_dataset["permanent_water"].isel(x=slice(10, 20), y=slice(0, 5)).sum()) == 50
    assert int(noaa_dataset["permanent_water"].isel(x=slice(10, 20), y=slice(5, 10)).sum()) == 0
