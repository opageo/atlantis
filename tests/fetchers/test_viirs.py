"""Tests for the VIIRS fetcher."""

from datetime import date, datetime, timezone
from pathlib import Path
from shutil import copyfile
from zipfile import ZipFile

import numpy as np
import pytest
import rasterio
import requests
from rasterio.transform import from_origin

from atlantis.fetchers.base import SearchResult
from atlantis.fetchers.viirs import (
    VIIRSFetcher,
    _date_range,
    _normalise_backend,
    _normalise_format,
)
from atlantis.models.event import FloodEvent

# ── Helpers ──────────────────────────────────────────────────────────────────


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


# ── Helper function tests ────────────────────────────────────────────────────


class TestNormaliseBackend:
    def test_valid_noaa(self):
        assert _normalise_backend("noaa_s3") == "noaa_s3"

    def test_valid_gmu(self):
        assert _normalise_backend("gmu_legacy") == "gmu_legacy"

    def test_case_insensitive(self):
        assert _normalise_backend("NOAA_S3") == "noaa_s3"

    def test_strips_whitespace(self):
        assert _normalise_backend("  gmu_legacy  ") == "gmu_legacy"

    def test_invalid_backend(self):
        with pytest.raises(ValueError, match="Unsupported VIIRS backend"):
            _nonexistent = _normalise_backend("nonexistent_backend")


class TestNormaliseFormat:
    def test_tif(self):
        assert _normalise_format("tif") == "tif"

    def test_tiff_alias(self):
        assert _normalise_format("tiff") == "tif"

    def test_nc_alias(self):
        with pytest.raises(NotImplementedError, match="not implemented yet"):
            _normalise_format("nc")

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Unsupported VIIRS format"):
            _normalise_format("csv")

    def test_shapefile_alias(self):
        with pytest.raises(NotImplementedError, match="not implemented yet"):
            _normalise_format("shapefile")


class TestDateRange:
    def test_single_day(self):
        d = datetime(2020, 7, 22, tzinfo=timezone.utc)
        result = _date_range(d, d)
        assert result == [d]

    def test_multi_day(self):
        start = datetime(2020, 7, 20, tzinfo=timezone.utc)
        end = datetime(2020, 7, 22, tzinfo=timezone.utc)
        result = _date_range(start, end)
        assert len(result) == 3
        assert result[0] == start
        assert result[-1] == end

    def test_empty_when_start_after_end(self):
        start = datetime(2020, 7, 22, tzinfo=timezone.utc)
        end = datetime(2020, 7, 20, tzinfo=timezone.utc)
        result = _date_range(start, end)
        assert result == []


# ── VIIRSFetcher constructor tests ───────────────────────────────────────────


class TestVIIRSFetcherInit:
    def test_defaults(self):
        fetcher = VIIRSFetcher()
        assert fetcher.backend_name == "noaa_s3"
        assert fetcher.classify is False
        assert fetcher.stream is False
        assert fetcher.data_format == "tif"
        assert fetcher.strategy == "peak"
        assert fetcher.keep_processed is True

    def test_classify_flag(self):
        fetcher = VIIRSFetcher(classify=True)
        assert fetcher.classify is True

    def test_stream_flag(self):
        fetcher = VIIRSFetcher(stream=True)
        assert fetcher.stream is True

    def test_backend_override(self):
        fetcher = VIIRSFetcher(backend="gmu_legacy")
        assert fetcher.backend_name == "gmu_legacy"

    def test_base_url_override(self):
        fetcher = VIIRSFetcher(base_url="https://custom.example.com")
        assert fetcher.base_url == "https://custom.example.com"

    def test_timeout_override(self):
        fetcher = VIIRSFetcher(timeout=60)
        assert fetcher.timeout == 60

    def test_aoi_path_exists(self):
        fetcher = VIIRSFetcher()
        assert fetcher.aoi_path.exists()
        assert fetcher.aoi_path.name == "viirs_aois.geojson"

    def test_backend_env_var_override(self, monkeypatch):
        """When ATLANTIS_VIIRS_BACKEND is set and config is reloaded, fetcher picks it up."""
        monkeypatch.setenv("ATLANTIS_VIIRS_BACKEND", "gmu_legacy")
        from atlantis.config import get_config, reload_config

        old_config = get_config.__globals__.get("_config")
        reload_config()
        try:
            fetcher = VIIRSFetcher()
            assert fetcher.backend_name == "gmu_legacy"
        finally:
            get_config.__globals__["_config"] = old_config

    def test_invalid_backend_raises(self):
        with pytest.raises(ValueError, match="Unsupported VIIRS backend"):
            VIIRSFetcher(backend="invalid_backend")

    def test_format_override(self):
        with pytest.raises(NotImplementedError, match="not implemented yet"):
            VIIRSFetcher(data_format="png")


# ── Search tests ─────────────────────────────────────────────────────────────


class TestVIIRSFetcherSearch:
    def _default_event(self):
        return FloodEvent(
            event_id="Yangtze_2020",
            bbox=(105.0, 28.0, 125.0, 38.0),
            start_date=date(2020, 7, 22),
            end_date=date(2020, 7, 22),
            sources=["viirs"],
        )

    def test_search_returns_intersecting_aoi_results(self, monkeypatch):
        fetcher = VIIRSFetcher()
        event = self._default_event()

        hrefs = [
            "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB077_v1r0_blend_s202007220000000_e202007222359590_c202205240401305.tif",
            "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB078_v1r0_blend_s202007220000000_e202007222359590_c202205240401363.tif",
        ]
        monkeypatch.setattr(fetcher.backend, "available_years", lambda _b, _f, _t: None)
        monkeypatch.setattr(fetcher.backend, "get_directory_links", lambda _base_url, _location, _timeout: hrefs)

        results = fetcher.search(event)
        assert len(results) == 2
        assert all(isinstance(r, SearchResult) for r in results)
        assert all(r.source_id == "viirs" for r in results)

    def test_search_no_intersecting_aois(self, monkeypatch):
        """Event bbox outside any AOI should return empty list."""
        fetcher = VIIRSFetcher()
        event = FloodEvent(
            event_id="Pacific_2020",
            bbox=(170.0, -10.0, 180.0, -5.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["viirs"],
        )
        results = fetcher.search(event)
        assert results == []

    def test_search_no_matching_entries(self, monkeypatch):
        """AOI found but backend returns empty directory listing."""
        fetcher = VIIRSFetcher()
        event = self._default_event()
        monkeypatch.setattr(fetcher.backend, "available_years", lambda _b, _f, _t: None)
        monkeypatch.setattr(fetcher.backend, "get_directory_links", lambda _b, _l, _t: [])

        results = fetcher.search(event)
        assert results == []

    def test_search_multi_day_event(self, monkeypatch):
        """Multi-day event expands into per-day search results."""
        fetcher = VIIRSFetcher()
        event = FloodEvent(
            event_id="Yangtze_2020",
            bbox=(105.0, 28.0, 125.0, 38.0),
            start_date=date(2020, 7, 21),
            end_date=date(2020, 7, 22),
            sources=["viirs"],
        )

        def mock_links(_base_url, _location, _timeout):
            return [
                "VIIRS-Flood-1day-GLB077_v1r0.tif",
            ]

        monkeypatch.setattr(fetcher.backend, "available_years", lambda _b, _f, _t: None)
        monkeypatch.setattr(fetcher.backend, "get_directory_links", mock_links)

        results = fetcher.search(event)
        # 2 AOIs × 2 days = 4 results (if grid intersects)
        # The exact number depends on AOI grid, but should have >1
        assert len(results) >= 2
        dates = {r.timestamp.date() for r in results}
        assert len(dates) == 2  # spans two days

    def test_search_gmu_backend(self, monkeypatch):
        fetcher = VIIRSFetcher(backend="gmu_legacy")
        event = self._default_event()
        hrefs = [
            "WATER_COM_VIIRS_Prj_SVI_d20200718_d20200722_4448_4448_35_005day_077.tif.zip",
        ]
        monkeypatch.setattr(fetcher.backend, "available_years", lambda _b, _f, _t: None)
        monkeypatch.setattr(fetcher.backend, "get_directory_links", lambda _b, _l, _t: hrefs)

        results = fetcher.search(event)
        assert len(results) == 1
        assert all(r.properties["backend"] == "gmu_legacy" for r in results)


# ── Diagnostics tests ────────────────────────────────────────────────────────


class TestVIIRSFetcherDiagnostics:
    """Verify last_diagnostics explains why search() returned what it did."""

    def _pakistan_event(self, year: int = 2022):
        return FloodEvent(
            event_id=f"Pakistan_{year}",
            bbox=(67.5, 26.0, 70.0, 29.5),
            start_date=date(year, 8, 28),
            end_date=date(year, 8, 30),
            sources=["viirs"],
        )

    def test_diagnostics_no_aoi_intersection(self):
        fetcher = VIIRSFetcher()
        event = FloodEvent(
            event_id="Pacific_2020",
            bbox=(170.0, -10.0, 180.0, -5.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["viirs"],
        )
        results = fetcher.search(event)
        assert results == []
        diag = fetcher.last_diagnostics
        assert diag is not None
        assert diag.backend == "noaa_s3"
        assert diag.missing_aoi_coverage is True
        assert diag.aoi_count == 0

    def test_diagnostics_year_coverage_gap_skips_listings(self, monkeypatch):
        """When backend declares coverage and year is missing, no listings are attempted."""
        fetcher = VIIRSFetcher()
        event = self._pakistan_event(2022)

        monkeypatch.setattr(
            fetcher.backend,
            "available_years",
            lambda _base, _fmt, _to: {2012, 2013, 2020, 2023, 2024},
        )

        listing_calls: list[str] = []

        def _fail_if_called(_base, location, _timeout):
            listing_calls.append(location)
            return []

        monkeypatch.setattr(fetcher.backend, "get_directory_links", _fail_if_called)

        results = fetcher.search(event)
        assert results == []
        assert listing_calls == [], "Backend should not be probed for years outside its coverage"

        diag = fetcher.last_diagnostics
        assert diag is not None
        assert diag.requested_years == {2022}
        assert diag.skipped_years == {2022}
        assert diag.year_coverage_gap is True
        assert diag.dates_probed == 0

    def test_diagnostics_all_listings_empty(self, monkeypatch):
        """When backend has coverage but every listing is empty, that fact is captured."""
        fetcher = VIIRSFetcher()
        event = self._pakistan_event(2023)

        monkeypatch.setattr(
            fetcher.backend,
            "available_years",
            lambda _base, _fmt, _to: {2023},
        )
        monkeypatch.setattr(fetcher.backend, "get_directory_links", lambda _b, _l, _t: [])

        results = fetcher.search(event)
        assert results == []
        diag = fetcher.last_diagnostics
        assert diag is not None
        assert diag.dates_probed == 3
        assert diag.dates_with_listings == 0
        assert diag.listings_all_empty is True

    def test_diagnostics_unknown_coverage_falls_back_to_probing(self, monkeypatch):
        """Backends returning None coverage must still probe every date."""
        fetcher = VIIRSFetcher(backend="gmu_legacy")
        event = self._pakistan_event(2022)

        # available_years returns None by default for GmuLegacyBackend, no patching needed.
        probe_count = {"n": 0}

        def _empty(_b, _l, _t):
            probe_count["n"] += 1
            return []

        monkeypatch.setattr(fetcher.backend, "get_directory_links", _empty)
        results = fetcher.search(event)
        assert results == []
        assert probe_count["n"] == 3
        diag = fetcher.last_diagnostics
        assert diag is not None
        assert diag.available_years is None
        assert diag.skipped_years == set()
        assert diag.dates_probed == 3

    def test_diagnostics_network_failure_is_graceful(self, monkeypatch):
        """Network errors during listing must not crash and must be surfaced via diagnostics."""
        fetcher = VIIRSFetcher(backend="gmu_legacy")
        event = self._pakistan_event(2022)

        def _raise(_b, _l, _t):
            raise requests.ConnectTimeout("Connection to jpssflood.gmu.edu timed out.")

        monkeypatch.setattr(fetcher.backend, "get_directory_links", _raise)

        results = fetcher.search(event)
        assert results == []
        diag = fetcher.last_diagnostics
        assert diag is not None
        assert diag.dates_probed == 3
        assert diag.network_failures == 3
        assert diag.network_unreachable is True
        assert diag.last_network_error is not None
        assert "jpssflood.gmu.edu" in diag.last_network_error


# ── Fetch tests ──────────────────────────────────────────────────────────────


class TestVIIRSFetcherFetch:
    def _default_event(self):
        return FloodEvent(
            event_id="Yangtze_2020",
            bbox=(105.0, 28.0, 125.0, 38.0),
            start_date=date(2020, 7, 22),
            end_date=date(2020, 7, 22),
            sources=["viirs"],
        )

    def test_fetch_empty_search_returns_empty(self, monkeypatch):
        fetcher = VIIRSFetcher()
        event = self._default_event()
        monkeypatch.setattr(fetcher, "search", lambda _e: [])

        results = fetcher.fetch(event, Path("/tmp/output"))
        assert results == []

    def test_fetch_stream_mode_skips_download(self, tmp_path, monkeypatch):
        """In stream mode, files should not be downloaded."""
        fetcher = VIIRSFetcher(stream=True)
        event = self._default_event()

        tile_data = np.full((10, 10), 170, dtype=np.uint8)

        tile1_tif = tmp_path / "tile_077.tif"
        _write_tile(tile1_tif, 105.0, 28.0, 115.0, 38.0, tile_data)

        search_results = [
            SearchResult(
                source_id="viirs",
                item_id="viirs:20200722:077",
                timestamp=datetime(2020, 7, 22, tzinfo=timezone.utc),
                bbox=(105.0, 28.0, 115.0, 38.0),
                url=tile1_tif.as_posix(),
                properties={
                    "aoi_id": 77,
                    "date": "20200722",
                    "filename": "tile_077.tif",
                    "backend": "noaa_s3",
                    "format": "tif",
                },
            ),
        ]
        monkeypatch.setattr(fetcher, "search", lambda _e: search_results)

        download_called = False

        def fake_download(*args, **kwargs):
            nonlocal download_called
            download_called = True

        monkeypatch.setattr("atlantis.fetchers.viirs.download_file", fake_download)

        results = fetcher.fetch(event, tmp_path / "stream_out")
        assert not download_called, "download_file should not be called in stream mode"
        assert len(results) <= 1  # may or may not produce results depending on URL format

    def test_fetch_classify(self, tmp_path, monkeypatch):
        """Verify classify=True fetches and produces flood_fraction output."""
        fetcher = VIIRSFetcher(classify=True)
        event = self._default_event()

        tile_data = np.random.randint(0, 200, (10, 10), dtype=np.uint8)
        tile_tif = tmp_path / "tile_077.tif"
        _write_tile(tile_tif, 105.0, 28.0, 115.0, 38.0, tile_data)

        tile_zip = tmp_path / "tile_077.tif.zip"
        _zip_file(tile_zip, tile_tif)

        search_results = [
            SearchResult(
                source_id="viirs",
                item_id="viirs:20200722:077",
                timestamp=datetime(2020, 7, 22, tzinfo=timezone.utc),
                bbox=(105.0, 28.0, 115.0, 38.0),
                url="https://example.com/tile_077.tif.zip",
                properties={
                    "aoi_id": 77,
                    "date": "20200722",
                    "filename": tile_zip.name,
                },
            ),
        ]
        monkeypatch.setattr(fetcher, "search", lambda _e: search_results)

        def fake_download(url, output_path=None, **_kwargs):
            copyfile(tile_zip, output_path)
            return output_path

        monkeypatch.setattr("atlantis.fetchers.viirs.download_file", fake_download)

        results = fetcher.fetch(event, tmp_path / "out")
        assert len(results) == 1

    def test_fetch_no_search_results(self, monkeypatch):
        fetcher = VIIRSFetcher()
        event = self._default_event()
        monkeypatch.setattr(fetcher, "search", lambda _e: [])

        results = fetcher.fetch(event, Path("/tmp/nonexistent"))
        assert results == []

    def test_fetch_strategy_peak_no_keep_processed(self, tmp_path, monkeypatch):
        """In-memory mode returns one peak-flood result and skips processed/ writes."""
        fetcher = VIIRSFetcher(classify=True, strategy="peak", keep_processed=False, stream=True)
        event = FloodEvent(
            event_id="Yangtze_2020",
            bbox=(105.0, 28.0, 125.0, 38.0),
            start_date=date(2020, 7, 21),
            end_date=date(2020, 7, 22),
            sources=["viirs"],
        )

        low_flood = np.zeros((10, 10), dtype=np.uint8)
        low_flood[0, 0] = 170
        high_flood = np.full((10, 10), 170, dtype=np.uint8)

        tile_low = tmp_path / "tile_low.tif"
        tile_high = tmp_path / "tile_high.tif"
        _write_tile(tile_low, 105.0, 28.0, 115.0, 38.0, low_flood)
        _write_tile(tile_high, 105.0, 28.0, 115.0, 38.0, high_flood)

        search_results = [
            SearchResult(
                source_id="viirs",
                item_id="viirs:20200721:077",
                timestamp=datetime(2020, 7, 21, tzinfo=timezone.utc),
                bbox=(105.0, 28.0, 115.0, 38.0),
                url=tile_low.as_posix(),
                properties={"aoi_id": 77, "date": "20200721", "filename": "tile_low.tif"},
            ),
            SearchResult(
                source_id="viirs",
                item_id="viirs:20200722:077",
                timestamp=datetime(2020, 7, 22, tzinfo=timezone.utc),
                bbox=(105.0, 28.0, 115.0, 38.0),
                url=tile_high.as_posix(),
                properties={"aoi_id": 77, "date": "20200722", "filename": "tile_high.tif"},
            ),
        ]
        monkeypatch.setattr(fetcher, "search", lambda _e: search_results)

        out_dir = tmp_path / "memory_out"
        results = fetcher.fetch(event, out_dir)

        assert len(results) == 1
        assert results[0].files == []
        assert results[0].dataset is not None
        assert results[0].date_token == "20200722"
        assert not (out_dir / "processed").exists()

        dataset = fetcher.to_dataset(results[0])
        assert "flood_fraction" in dataset.data_vars


# ── to_dataset tests ─────────────────────────────────────────────────────────


class TestVIIRSToDataset:
    def test_to_dataset_from_fetch_result(self, tmp_path):
        """Test dataset conversion from three-component FetchResult."""
        fetcher = VIIRSFetcher(classify=True)

        fe = np.full((10, 10), 170, dtype=np.uint8)
        qm = np.ones((10, 10), dtype=np.uint8)
        pw = np.zeros((10, 10), dtype=np.uint8)

        fe_path = tmp_path / "test_flood_fraction.tif"
        qm_path = tmp_path / "test_quality_mask.tif"
        pw_path = tmp_path / "test_permanent_water.tif"

        for path, data in [(fe_path, fe), (qm_path, qm), (pw_path, pw)]:
            _write_tile(path, 20.0, 30.0, 21.0, 31.0, data)

        from atlantis.fetchers.base import FetchResult
        from atlantis.models.metadata import TileMetadata

        result = FetchResult(
            event_id="test_event",
            source_id="viirs",
            files=[fe_path, qm_path, pw_path],
            metadata=TileMetadata(
                event_id="test_event",
                source_id="viirs",
                fetch_timestamp=datetime(2020, 7, 22, tzinfo=timezone.utc),
                bbox=(20.0, 30.0, 21.0, 31.0),
            ),
        )

        dataset = fetcher.to_dataset(result)
        assert "flood_fraction" in dataset.data_vars
        assert "quality_mask" in dataset.data_vars
        assert "permanent_water" in dataset.data_vars
        assert dataset.attrs["source_id"] == "viirs"
        assert dataset.attrs["event_id"] == "test_event"


# ── Existing tests (preserved) ───────────────────────────────────────────────


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
    # Code 99 ("NormalWater") is the embedded NOAA legend's permanent-water class;
    # bottom half overwritten with cloud (30).
    tile2_data = np.full((10, 10), 99, dtype=np.uint8)
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

    assert set(dataset.data_vars) == {"flood_fraction", "quality_mask", "permanent_water"}
    assert dataset["flood_fraction"].dtype == np.float32
    assert dataset["quality_mask"].dtype == np.uint8
    assert dataset["permanent_water"].dtype == np.uint8
    assert float(dataset["flood_fraction"].sum()) > 0
    # Cloud pixels (code 30) → quality=0; flood pixels (170) → quality=1
    assert int(dataset["quality_mask"].min()) == 0
    assert int(dataset["quality_mask"].max()) == 1
    assert int(dataset["permanent_water"].max()) == 1
    # Permanent water pixels (code 99) are valid observations → quality=1, not 0
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
    # Tile 078: top half = permanent water (code 99, "NormalWater" per embedded
    # NOAA TIFF legend), bottom half = cloud (30).
    tile2_data = np.full((10, 10), 99, dtype=np.uint8)
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
    assert set(noaa_dataset.data_vars) == {"flood_fraction", "quality_mask", "permanent_water"}
    assert set(gmu_dataset.data_vars) == {"flood_fraction", "quality_mask", "permanent_water"}

    # Same array shapes (mosaic of two 10×10 tiles → 10×20 for 1°×1° bbox)
    expected_shape = (10, 20)
    for var in ("flood_fraction", "quality_mask", "permanent_water"):
        noaa_arr = noaa_dataset[var]
        gmu_arr = gmu_dataset[var]
        assert noaa_arr.shape == expected_shape, f"{var} shape mismatch (noaa)"
        assert gmu_arr.shape == expected_shape, f"{var} shape mismatch (gmu)"
        assert noaa_arr.shape == gmu_arr.shape, f"{var} shape differs between backends"

    # Same dtypes per variable (as documented in docs/viirs/overview.md)
    assert noaa_dataset["flood_fraction"].dtype == np.float32
    assert gmu_dataset["flood_fraction"].dtype == np.float32
    assert noaa_dataset["quality_mask"].dtype == np.uint8
    assert gmu_dataset["quality_mask"].dtype == np.uint8
    assert noaa_dataset["permanent_water"].dtype == np.uint8
    assert gmu_dataset["permanent_water"].dtype == np.uint8

    # Same pixel values — the raster arrays are byte-identical
    for var in ("flood_fraction", "quality_mask", "permanent_water"):
        assert (noaa_dataset[var].values == gmu_dataset[var].values).all(), f"{var} values differ between backends"

    # Semantic correctness: left half (077) is all flood, right half (078) is mixed
    # Columns 0-9 = tile 077 (flood code 170 → flood_fraction = 0.70, 100 pixels)
    assert abs(float(noaa_dataset["flood_fraction"].isel(x=slice(0, 10)).sum()) - 70.0) < 1e-3, (
        "left tile should be all flood"
    )
    # Columns 10-19 tile 078 rows 0-4 = permanent water (17 → flood_fraction = 0.0)
    assert float(noaa_dataset["flood_fraction"].isel(x=slice(10, 20), y=slice(0, 5)).sum()) == 0.0
    # Columns 10-19 tile 078 rows 5-9 = cloud (30 → flood_fraction = 0.0)
    assert float(noaa_dataset["flood_fraction"].isel(x=slice(10, 20), y=slice(5, 10)).sum()) == 0.0

    # Quality mask: 1 = valid clear-sky observation, 0 = fill or cloud only.
    # Permanent water (17) is a valid sensor observation → quality=1.
    assert int(noaa_dataset["quality_mask"].isel(x=slice(0, 10)).sum()) == 100  # flood → valid
    assert int(noaa_dataset["quality_mask"].isel(x=slice(10, 20), y=slice(0, 5)).sum()) == 50  # perm water → valid
    assert int(noaa_dataset["quality_mask"].isel(x=slice(10, 20), y=slice(5, 10)).sum()) == 0  # cloud → invalid

    # Permanent water: right-half top rows only
    assert int(noaa_dataset["permanent_water"].isel(x=slice(0, 10)).sum()) == 0
    assert int(noaa_dataset["permanent_water"].isel(x=slice(10, 20), y=slice(0, 5)).sum()) == 50
    assert int(noaa_dataset["permanent_water"].isel(x=slice(10, 20), y=slice(5, 10)).sum()) == 0


# ── Peak-window / subsampling integration tests ───────────────────────────────


def _make_search_results_for_dates(
    tmp_path: Path,
    date_tokens: list[str],
    flood_counts: list[int],
    bbox: tuple[float, float, float, float] = (105.0, 28.0, 115.0, 38.0),
) -> tuple[list[SearchResult], dict]:
    """Build synthetic tiles + SearchResult list.  Returns (results, tif_map)."""
    results = []
    tif_map = {}
    for token, count in zip(date_tokens, flood_counts):
        data = np.zeros((10, 10), dtype=np.uint8)
        data.ravel()[:count] = 170  # flood code
        tif_path = tmp_path / f"tile_{token}.tif"
        _write_tile(tif_path, bbox[0], bbox[1], bbox[2], bbox[3], data)
        tif_map[token] = tif_path
        dt_year, dt_month, dt_day = int(token[:4]), int(token[4:6]), int(token[6:8])
        results.append(
            SearchResult(
                source_id="viirs",
                item_id=f"viirs:{token}:077",
                timestamp=datetime(dt_year, dt_month, dt_day, tzinfo=timezone.utc),
                bbox=bbox,
                url=tif_path.as_posix(),
                properties={"aoi_id": 77, "date": token, "filename": tif_path.name},
            )
        )
    return results, tif_map


class TestPeakWindowIntegration:
    """Integration tests: VIIRSFetcher with peak_days_before/after and max_observations."""

    def _event(self):
        return FloodEvent(
            event_id="TestEvent",
            bbox=(105.0, 28.0, 115.0, 38.0),
            start_date=date(2020, 7, 1),
            end_date=date(2020, 7, 7),
            sources=["viirs"],
        )

    # 7 dates; peak on day 3 (index 2) with 80 flood pixels
    _date_tokens = ["20200701", "20200702", "20200703", "20200704", "20200705", "20200706", "20200707"]
    _flood_counts = [10, 30, 80, 50, 20, 5, 2]

    def test_strategy_all_no_window_returns_all_dates(self, tmp_path, monkeypatch):
        fetcher = VIIRSFetcher(classify=True, strategy="all", keep_processed=False, stream=True)
        sr, _ = _make_search_results_for_dates(tmp_path, self._date_tokens, self._flood_counts)
        monkeypatch.setattr(fetcher, "search", lambda _e: sr)
        results = fetcher.fetch(self._event(), tmp_path / "out")
        # strategy=all + no window → one result per date (in-memory)
        assert len(results) == 7

    def test_window_filters_to_correct_dates_strategy_all(self, tmp_path, monkeypatch):
        """±2 days around peak (day 3) → days 1–5 (5 dates)."""
        fetcher = VIIRSFetcher(
            classify=True,
            strategy="all",
            keep_processed=True,
            stream=True,
            peak_days_before=2,
            peak_days_after=2,
        )
        sr, _ = _make_search_results_for_dates(tmp_path, self._date_tokens, self._flood_counts)
        monkeypatch.setattr(fetcher, "search", lambda _e: sr)
        out_dir = tmp_path / "out"
        results = fetcher.fetch(self._event(), out_dir)
        assert len(results) == 5
        returned_tokens = {r.date_token for r in results}
        assert returned_tokens == {"20200701", "20200702", "20200703", "20200704", "20200705"}
        # Only 5 dates written to processed/
        processed_files = list((out_dir / "processed").glob("*_flood_fraction.tif"))
        assert len(processed_files) == 5

    def test_max_observations_post_priority(self, tmp_path, monkeypatch):
        """Window ±3, max 3, post priority → peak + 2 post-peak dates."""
        fetcher = VIIRSFetcher(
            classify=True,
            strategy="all",
            keep_processed=False,
            stream=True,
            peak_days_before=3,
            peak_days_after=3,
            max_observations=3,
            peak_priority="post",
        )
        sr, _ = _make_search_results_for_dates(tmp_path, self._date_tokens, self._flood_counts)
        monkeypatch.setattr(fetcher, "search", lambda _e: sr)
        results = fetcher.fetch(self._event(), tmp_path / "out")
        # strategy=all, no keep_processed → one in-memory result per surviving date
        assert len(results) == 3
        returned_tokens = {r.date_token for r in results}
        assert "20200703" in returned_tokens  # peak (day 3)
        assert "20200704" in returned_tokens  # post +1
        assert "20200705" in returned_tokens  # post +2

    def test_max_observations_with_keep_processed_writes_only_survivors(self, tmp_path, monkeypatch):
        """With keep_processed and max_observations, only surviving dates appear in processed/."""
        fetcher = VIIRSFetcher(
            classify=True,
            strategy="all",
            keep_processed=True,
            stream=True,
            peak_days_before=3,
            peak_days_after=3,
            max_observations=3,
            peak_priority="post",
        )
        sr, _ = _make_search_results_for_dates(tmp_path, self._date_tokens, self._flood_counts)
        monkeypatch.setattr(fetcher, "search", lambda _e: sr)
        out_dir = tmp_path / "out"
        results = fetcher.fetch(self._event(), out_dir)
        assert len(results) == 3
        # Peak is day 3; post priority → days 3,4,5
        returned_tokens = {r.date_token for r in results}
        assert "20200703" in returned_tokens  # peak
        assert "20200704" in returned_tokens  # post +1
        assert "20200705" in returned_tokens  # post +2
        # processed/ should contain exactly 3 flood_fraction files
        processed_files = sorted((out_dir / "processed").glob("*_flood_fraction.tif"))
        assert len(processed_files) == 3

    def test_no_window_flags_is_noop(self, tmp_path, monkeypatch):
        """With zero window flags, all dates pass through unchanged."""
        fetcher = VIIRSFetcher(
            classify=True,
            strategy="all",
            keep_processed=True,
            stream=True,
            peak_days_before=0,
            peak_days_after=0,
            max_observations=0,
        )
        sr, _ = _make_search_results_for_dates(tmp_path, self._date_tokens, self._flood_counts)
        monkeypatch.setattr(fetcher, "search", lambda _e: sr)
        out_dir = tmp_path / "out"
        results = fetcher.fetch(self._event(), out_dir)
        assert len(results) == 7
        processed_files = list((out_dir / "processed").glob("*_flood_fraction.tif"))
        assert len(processed_files) == 7

    def test_invalid_peak_priority_raises(self):
        with pytest.raises(ValueError, match="peak_priority"):
            VIIRSFetcher(peak_priority="unknown")

    def test_negative_peak_days_raises(self):
        with pytest.raises(ValueError, match="peak_days_before"):
            VIIRSFetcher(peak_days_before=-1)
        with pytest.raises(ValueError, match="peak_days_after"):
            VIIRSFetcher(peak_days_after=-1)

    def test_negative_max_observations_raises(self):
        with pytest.raises(ValueError, match="max_observations"):
            VIIRSFetcher(max_observations=-1)
