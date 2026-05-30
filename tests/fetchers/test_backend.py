"""Tests for VIIRS backend implementations."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from atlantis.fetchers.viirs.backend import (
    GmuLegacyBackend,
    ListingLocation,
    NoaaS3Backend,
    ViirsBackend,
)


class TestViirsBackendABC:
    """Verify the abstract base class contract."""

    def test_abstract_methods(self):
        """ViirsBackend cannot be instantiated directly."""
        with pytest.raises(TypeError):
            ViirsBackend()  # type: ignore[abstract]


class TestListingLocation:
    def test_defaults(self):
        loc = ListingLocation(locator="https://example.com/path/", date_token="20200722")
        assert loc.locator == "https://example.com/path/"
        assert loc.date_token == "20200722"

    def test_repr(self):
        loc = ListingLocation(locator="https://example.com/path/", date_token="20200101")
        r = repr(loc)
        assert "ListingLocation" in r
        assert "https://example.com" in r


class TestNoaaS3Backend:
    def test_listing_location(self):
        backend = NoaaS3Backend()
        dt = datetime(2020, 7, 22, tzinfo=timezone.utc)
        loc = backend.get_listing_location(
            base_url="https://noaa-jpss.s3.amazonaws.com/",
            event_date=dt,
            data_format="tif",
        )
        assert isinstance(loc, ListingLocation)
        assert loc.locator == "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/"
        assert loc.date_token == "20200722"

    def test_listing_location_default_url(self):
        backend = NoaaS3Backend()
        dt = datetime(2020, 7, 22, tzinfo=timezone.utc)
        loc = backend.get_listing_location(
            base_url="https://noaa-jpss.s3.amazonaws.com/",
            event_date=dt,
            data_format="tif",
        )
        assert loc.locator.startswith("JPSS_Blended_Products/")

    def test_listing_location_netcdf(self):
        backend = NoaaS3Backend()
        dt = datetime(2023, 6, 15, tzinfo=timezone.utc)
        loc = backend.get_listing_location(
            base_url="https://noaa-jpss.s3.amazonaws.com/",
            event_date=dt,
            data_format="netcdf",
        )
        assert "NETCDF" in loc.locator.upper()

    def test_find_remote_filename(self):
        backend = NoaaS3Backend()
        entries = [
            "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB077_v1r0_blend_s202007220000000_e202007222359590_c202205240401305.tif",
            "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB091_v1r0_blend_s202007220000000_e202007222359590_c202205240402523.tif",
            "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB092_v1r0_blend_s202007220000000_e202007222359590_c202205240402593.tif",
        ]
        found = backend.find_remote_filename(aoi_id=77, entries=entries)
        assert found is not None
        assert "GLB077" in found

    def test_find_remote_filename_missing(self):
        backend = NoaaS3Backend()
        entries = [
            "JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB091_v1r0_blend_s202007220000000_e202007222359590_c202205240402523.tif"
        ]
        found = backend.find_remote_filename(aoi_id=999, entries=entries)
        assert found is None

    def test_build_result_url(self):
        backend = NoaaS3Backend()
        url = backend.build_result_url(
            base_url="https://noaa-jpss.s3.amazonaws.com",
            listing_location="JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/",
            filename="VIIRS-Flood-1day-GLB077_v1r0_blend_s202007220000000_e202007222359590_c202205240401305.tif",
        )
        assert (
            url
            == "https://noaa-jpss.s3.amazonaws.com/JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/VIIRS-Flood-1day-GLB077_v1r0_blend_s202007220000000_e202007222359590_c202205240401305.tif"
        )

    def test_get_directory_links_no_timeout(self):
        """Should call through to the actual listing with a timeout."""
        backend = NoaaS3Backend()
        # Can't easily test the S3 listing without network, but
        # verify the method signature accepts the expected args.
        import unittest.mock as mock

        mock_response = mock.Mock()
        mock_response.status_code = 404

        with mock.patch("requests.get", return_value=mock_response):
            results = backend.get_directory_links(
                base_url="https://noaa-jpss.s3.amazonaws.com/",
                location="JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/07/22/",
                timeout=120,
            )
            assert results == []


class TestGmuLegacyBackend:
    def test_listing_location(self):
        backend = GmuLegacyBackend()
        dt = datetime(2020, 7, 22, tzinfo=timezone.utc)
        loc = backend.get_listing_location(
            base_url="https://floodmap.gmu.edu/gmuv2_archive/",
            event_date=dt,
            data_format="shapezip",
        )
        assert isinstance(loc, ListingLocation)
        assert "2020" in loc.locator

    def test_listing_location_default_url(self):
        backend = GmuLegacyBackend()
        dt = datetime(2020, 7, 22, tzinfo=timezone.utc)
        loc = backend.get_listing_location(
            base_url="https://floodmap.gmu.edu/gmuv2_archive/",
            event_date=dt,
            data_format="tif",
        )
        assert "gmu" in loc.locator.lower()

    def test_find_remote_filename(self):
        backend = GmuLegacyBackend()
        entries = [
            "WATER_COM_VIIRS_Prj_SVI_d20200718_d20200722_4448_4448_35_005day_077.tif.zip",
            "WATER_COM_VIIRS_Prj_SVI_d20200718_d20200722_4448_4448_34_005day_078.tif.zip",
        ]
        found = backend.find_remote_filename(aoi_id=77, entries=entries)
        assert found is not None
        assert "_077" in found

    def test_find_remote_filename_missing(self):
        backend = GmuLegacyBackend()
        entries = ["WATER_COM_VIIRS_Prj_SVI_d20200718_d20200722_4448_4448_34_005day_078.tif.zip"]
        found = backend.find_remote_filename(aoi_id=999, entries=entries)
        assert found is None

    def test_build_result_url(self):
        backend = GmuLegacyBackend()
        url = backend.build_result_url(
            base_url="https://floodmap.gmu.edu/gmuv2_archive/",
            listing_location="https://floodmap.gmu.edu/gmuv2_archive/20200722/tif/",
            filename="WATER_COM_VIIRS_Prj_SVI_d20200718_d20200722_4448_4448_35_005day_077.tif.zip",
        )
        assert (
            url
            == "https://floodmap.gmu.edu/gmuv2_archive/20200722/tif/WATER_COM_VIIRS_Prj_SVI_d20200718_d20200722_4448_4448_35_005day_077.tif.zip"
        )
