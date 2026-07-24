"""Tests for MODIS backend implementations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

from atlantis.fetchers.modis.backend import (
    LaadsHdf4Backend,
    LanceGeotiffBackend,
    ListingLocation,
    MissingEarthdataTokenError,
    ModisBackend,
    ModisListingEntry,
    earthdata_auth_headers,
    get_backend,
    get_earthdata_token,
    list_backends,
    parse_prod_timestamp,
)


@pytest.fixture
def earthdata_token(monkeypatch):
    monkeypatch.setenv("EARTHDATA_TOKEN", "test-token")
    return "test-token"


@pytest.fixture
def no_earthdata_token(monkeypatch):
    monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)


class TestModisBackendABC:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            ModisBackend()  # type: ignore[abstract]


class TestEarthdataToken:
    def test_get_returns_token(self, earthdata_token):
        assert get_earthdata_token() == "test-token"

    def test_get_raises_when_missing(self, no_earthdata_token):
        with pytest.raises(MissingEarthdataTokenError):
            get_earthdata_token()

    def test_headers_format(self, earthdata_token):
        headers = earthdata_auth_headers()
        assert headers == {"Authorization": "Bearer test-token"}


class TestParseProdTimestamp:
    def test_lance_filename(self):
        name = "MCDWD_L3_F2_NRT.A2026032.h09v05.061.2026032142200.tif"
        assert parse_prod_timestamp(name) == "2026032142200"

    def test_legacy_laads(self):
        # No production timestamp in the legacy LAADS form.
        name = "MCDWD_L3.A2024235.h24v05.061.hdf"
        assert parse_prod_timestamp(name) is None


class TestListingLocation:
    def test_defaults(self):
        loc = ListingLocation(locator="archive/allData/61/foo/", date_token="20240722")
        assert loc.date_token == "20240722"


class TestLanceGeotiffBackend:
    def test_listing_location(self):
        backend = LanceGeotiffBackend()
        dt = datetime(2026, 2, 1, tzinfo=timezone.utc)
        loc = backend.get_listing_location("https://nrt3.modaps.eosdis.nasa.gov", dt, "F2")
        assert loc.date_token == "20260201"
        assert loc.locator == "archive/allData/61/MCDWD_L3_F2_NRT/2026/032/"

    def test_listing_location_per_composite(self):
        backend = LanceGeotiffBackend()
        dt = datetime(2026, 2, 1, tzinfo=timezone.utc)
        for comp in ("F1", "F1C", "F2", "F3"):
            loc = backend.get_listing_location("https://nrt3.modaps.eosdis.nasa.gov", dt, comp)
            assert f"MCDWD_L3_{comp}_NRT" in loc.locator

    def test_temporal_range(self):
        assert LanceGeotiffBackend._temporal_range("20260201") == "2026-032"

    def test_find_filename_matches_tile_and_composite(self):
        backend = LanceGeotiffBackend()
        entries = [
            ModisListingEntry(
                filename="MCDWD_L3_F2_NRT.A2026032.h09v05.061.2026032142200.tif",
                prod_timestamp="2026032142200",
            ),
            ModisListingEntry(
                filename="MCDWD_L3_F2_NRT.A2026032.h10v05.061.2026032142201.tif",
                prod_timestamp="2026032142201",
            ),
        ]
        match = backend.find_remote_filename(9, 5, "F2", entries)
        assert match is not None
        assert match.filename.startswith("MCDWD_L3_F2_NRT.A2026032.h09v05")

    def test_find_filename_returns_none_when_absent(self):
        backend = LanceGeotiffBackend()
        entries = [ModisListingEntry(filename="MCDWD_L3_F2_NRT.A2026032.h09v05.061.2026032142200.tif")]
        assert backend.find_remote_filename(99, 99, "F2", entries) is None

    def test_build_result_url_uses_entry_url_when_present(self):
        backend = LanceGeotiffBackend()
        loc = ListingLocation(locator="archive/allData/61/MCDWD_L3_F2_NRT/2026/032/", date_token="20260201")
        entry = ModisListingEntry(filename="foo.tif", url="https://example.com/foo.tif")
        url = backend.build_result_url("https://nrt3.modaps.eosdis.nasa.gov", loc, entry)
        assert url == "https://example.com/foo.tif"

    def test_build_result_url_synthesises_when_missing(self):
        backend = LanceGeotiffBackend()
        loc = ListingLocation(locator="archive/allData/61/MCDWD_L3_F2_NRT/2026/032/", date_token="20260201")
        entry = ModisListingEntry(filename="foo.tif")
        url = backend.build_result_url("https://nrt3.modaps.eosdis.nasa.gov", loc, entry)
        assert url == "https://nrt3.modaps.eosdis.nasa.gov/archive/allData/61/MCDWD_L3_F2_NRT/2026/032/foo.tif"

    def test_parse_json_listing_handles_content_wrapper(self):
        download_link = (
            "/archive/allData/61/MCDWD_L3_F2_NRT/2026/032/MCDWD_L3_F2_NRT.A2026032.h09v05.061.2026032142200.tif"
        )
        payload = json.dumps(
            {
                "content": [
                    {
                        "name": "MCDWD_L3_F2_NRT.A2026032.h09v05.061.2026032142200.tif",
                        "downloadsLink": download_link,
                    }
                ]
            }
        )
        entries = LanceGeotiffBackend._parse_json_listing(payload, base_url="https://nrt3.modaps.eosdis.nasa.gov")
        assert len(entries) == 1
        assert entries[0].prod_timestamp == "2026032142200"
        assert entries[0].url and entries[0].url.startswith("https://nrt3.")

    def test_parse_json_listing_handles_top_level_list(self):
        payload = json.dumps([{"name": "MCDWD_L3_F2_NRT.A2026032.h09v05.061.2026032142200.tif"}])
        entries = LanceGeotiffBackend._parse_json_listing(payload, base_url="https://nrt3.modaps.eosdis.nasa.gov")
        assert len(entries) == 1

    def test_parse_json_listing_malformed_returns_empty(self):
        entries = LanceGeotiffBackend._parse_json_listing(
            "{not valid json", base_url="https://nrt3.modaps.eosdis.nasa.gov"
        )
        assert entries == []

    def test_get_directory_listing_falls_back_to_backup(self, earthdata_token):
        backend = LanceGeotiffBackend(backup_base_url="https://nrt4.modaps.eosdis.nasa.gov")
        loc = backend.get_listing_location(
            "https://nrt3.modaps.eosdis.nasa.gov",
            datetime(2026, 2, 1, tzinfo=timezone.utc),
            "F2",
        )

        good = MagicMock()
        good.status_code = 200
        good.text = json.dumps({"content": [{"name": "MCDWD_L3_F2_NRT.A2026032.h09v05.061.2026032142200.tif"}]})
        good.raise_for_status = MagicMock()

        # First call (nrt3) raises; second call (nrt4) succeeds.
        with patch(
            "atlantis.fetchers.modis.backend.requests.get",
            side_effect=[requests.ConnectionError("nrt3 down"), good],
        ) as mock_get:
            entries = backend.get_directory_listing("https://nrt3.modaps.eosdis.nasa.gov", loc, timeout=5)

        assert mock_get.call_count == 2
        assert len(entries) == 1


class TestLaadsHdf4Backend:
    @pytest.fixture(autouse=True)
    def _bypass_hdf4_check(self, monkeypatch):
        # Avoid the GDAL HDF4 driver assertion in unit tests; the constructor
        # is exercised separately in test_init_fails_when_hdf4_missing.
        monkeypatch.setattr(
            LaadsHdf4Backend,
            "_verify_hdf4_driver",
            staticmethod(lambda: None),
        )

    def test_listing_location_reprocessed(self):
        backend = LaadsHdf4Backend()
        dt = datetime(2024, 8, 22, tzinfo=timezone.utc)
        loc = backend.get_listing_location("https://ladsweb.modaps.eosdis.nasa.gov", dt, "F2")
        assert "MCDWD_L3" in loc.locator
        assert "MCDWD_L3_NRT" not in loc.locator

    def test_listing_location_archived_nrt(self):
        backend = LaadsHdf4Backend()
        dt = datetime(2026, 5, 1, tzinfo=timezone.utc)
        loc = backend.get_listing_location("https://ladsweb.modaps.eosdis.nasa.gov", dt, "F2")
        assert "MCDWD_L3_NRT" in loc.locator

    def test_find_filename_matches_either_shortname(self):
        backend = LaadsHdf4Backend()
        entries = [
            ModisListingEntry(filename="MCDWD_L3.A2024235.h24v05.061.hdf"),
            ModisListingEntry(filename="MCDWD_L3_NRT.A2026032.h09v05.061.2026032142200.hdf"),
        ]
        m1 = backend.find_remote_filename(24, 5, "F2", entries)
        m2 = backend.find_remote_filename(9, 5, "F2", entries)
        assert m1 is not None and "MCDWD_L3.A2024235" in m1.filename
        assert m2 is not None and "MCDWD_L3_NRT.A2026032" in m2.filename

    def test_available_years_includes_reprocessed_range(self):
        backend = LaadsHdf4Backend()
        years = backend.available_years("https://ladsweb.modaps.eosdis.nasa.gov", timeout=5)
        assert years is not None
        assert 2003 in years
        assert 2025 in years

    def test_get_directory_listing_parses_html(self, earthdata_token):
        backend = LaadsHdf4Backend()
        loc = backend.get_listing_location(
            "https://ladsweb.modaps.eosdis.nasa.gov",
            datetime(2024, 8, 22, tzinfo=timezone.utc),
            "F2",
        )
        html = (
            "<html><body>"
            '<a href="MCDWD_L3.A2024235.h24v05.061.hdf">MCDWD_L3.A2024235.h24v05.061.hdf</a>'
            '<a href="MCDWD_L3.A2024235.h24v05.061.hdf.met">.met</a>'
            "</body></html>"
        )
        response = MagicMock(status_code=200, text=html)
        response.raise_for_status = MagicMock()
        with patch("atlantis.fetchers.modis.backend.requests.get", return_value=response) as mock_get:
            entries = backend.get_directory_listing("https://ladsweb.modaps.eosdis.nasa.gov", loc, timeout=5)
        assert mock_get.called
        assert len(entries) == 1  # the .met sidecar is filtered out
        assert entries[0].filename == "MCDWD_L3.A2024235.h24v05.061.hdf"


class TestRegistry:
    def test_list_backends_includes_both(self):
        names = list_backends()
        assert "lance_geotiff" in names
        assert "laads_hdf4" in names

    def test_get_backend_lance(self):
        b = get_backend("lance_geotiff")
        assert isinstance(b, LanceGeotiffBackend)

    def test_get_backend_unknown_raises(self):
        with pytest.raises(ValueError):
            get_backend("nonexistent")
