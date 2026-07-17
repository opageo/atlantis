"""Unit tests for atlantis.fetchers.modis.catalog (LAADS → Parquet inventory)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import requests

from atlantis.fetchers.modis.catalog import (
    _laads_directory_url,
    _laads_shortname_for_year,
    _list_tiles_for_date,
    _parse_hv_from_modis_filename,
    build_catalog,
)


class TestLaadsShortname:
    def test_reprocessed_before_2025(self):
        assert _laads_shortname_for_year(2024) == "MCDWD_L3"

    def test_reprocessed_2025(self):
        assert _laads_shortname_for_year(2025) == "MCDWD_L3"

    def test_nrt_after_2025(self):
        assert _laads_shortname_for_year(2026) == "MCDWD_L3_NRT"


class TestLaadsDirectoryUrl:
    def test_url_format(self):
        url = _laads_directory_url("2024-08-22")
        assert "ladsweb.modaps.eosdis.nasa.gov" in url
        assert "MCDWD_L3" in url
        assert "2024" in url
        # DOY for Aug 22 2024 is 235
        assert "/235/" in url

    def test_url_nrt_year(self):
        url = _laads_directory_url("2026-01-01")
        assert "MCDWD_L3_NRT" in url

    def test_doy_correctness(self):
        # Jan 1 = DOY 001
        assert "/001/" in _laads_directory_url("2024-01-01")
        # Dec 31 2024 (leap year) = DOY 366
        assert "/366/" in _laads_directory_url("2024-12-31")


class TestParseHvFromModisFilename:
    def test_standard_filename(self):
        assert _parse_hv_from_modis_filename("MCDWD_L3.A2024235.h24v05.061.hdf") == (24, 5)

    def test_single_digit_padded(self):
        assert _parse_hv_from_modis_filename("MCDWD_L3.A2024235.h09v05.061.hdf") == (9, 5)

    def test_nrt_filename(self):
        assert _parse_hv_from_modis_filename("MCDWD_L3_F2_NRT.A2026032.h09v05.061.2026032142200.tif") == (9, 5)

    def test_no_match_returns_none(self):
        assert _parse_hv_from_modis_filename("not_a_modis_file.tif") is None

    def test_no_hv_pattern_returns_none(self):
        assert _parse_hv_from_modis_filename("MCDWD_L3.A2024235.061.hdf") is None


class TestListTilesForDate:
    """Tests for _list_tiles_for_date with mocked HTTP."""

    _SAMPLE_HTML = """
    <html><body>
    <a href="MCDWD_L3.A2024235.h24v05.061.hdf">h24v05</a>
    <a href="MCDWD_L3.A2024235.h09v05.061.hdf">h09v05</a>
    <a href="MCDWD_L3.A2024235.h24v05.061.hdf">duplicate</a>
    <a href="some_other_file.txt">not a tile</a>
    </body></html>
    """

    def _mock_response(self, status_code=200, text=_SAMPLE_HTML):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        resp.raise_for_status = MagicMock()
        return resp

    @patch("atlantis.fetchers.modis.catalog.requests.get")
    @patch("atlantis.fetchers.modis.catalog.retry_request")
    def test_parses_tiles_from_html(self, mock_retry, mock_get):
        mock_retry.return_value = self._mock_response()
        tiles = _list_tiles_for_date("2024-08-22", {"Authorization": "Bearer token"})
        assert len(tiles) == 2  # duplicate h24v05 is deduplicated
        assert {t["h"] for t in tiles} == {24, 9}
        assert all(t["v"] == 5 for t in tiles)

    @patch("atlantis.fetchers.modis.catalog.retry_request")
    def test_404_returns_empty_list(self, mock_retry):
        resp = MagicMock()
        resp.status_code = 404
        resp.raise_for_status = MagicMock()
        mock_retry.return_value = resp
        tiles = _list_tiles_for_date("2024-08-22", {})
        assert tiles == []

    @patch("atlantis.fetchers.modis.catalog.retry_request")
    def test_task_id_format(self, mock_retry):
        mock_retry.return_value = self._mock_response()
        tiles = _list_tiles_for_date("2024-08-22", {})
        for t in tiles:
            assert t["task_id"].startswith("modis-20240822-h")
            assert "v05" in t["task_id"]

    @patch("atlantis.fetchers.modis.catalog.retry_request")
    def test_source_uri_contains_full_url(self, mock_retry):
        mock_retry.return_value = self._mock_response()
        tiles = _list_tiles_for_date("2024-08-22", {})
        for t in tiles:
            assert t["source_uri"].startswith("https://ladsweb.modaps.eosdis.nasa.gov/")
            assert t["source_uri"].endswith(".hdf")

    @patch("atlantis.fetchers.modis.catalog.retry_request")
    def test_empty_html_returns_empty(self, mock_retry):
        mock_retry.return_value = self._mock_response(text="<html><body></body></html>")
        tiles = _list_tiles_for_date("2024-08-22", {})
        assert tiles == []


class TestBuildCatalog:
    """Tests for build_catalog with mocked HTTP and filesystem."""

    @patch("atlantis.fetchers.modis.catalog.earthdata_auth_headers")
    @patch("atlantis.fetchers.modis.catalog._list_tiles_for_date")
    def test_builds_catalog_and_writes_parquet(self, mock_list, mock_auth, tmp_path):
        mock_auth.return_value = {"Authorization": "Bearer token"}
        mock_list.return_value = [
            {
                "date": "2024-08-22",
                "h": 24,
                "v": 5,
                "task_id": "modis-20240822-h24v05",
                "source_uri": "https://example.com/h24v05.hdf",
            },
            {
                "date": "2024-08-22",
                "h": 9,
                "v": 5,
                "task_id": "modis-20240822-h09v05",
                "source_uri": "https://example.com/h09v05.hdf",
            },
        ]
        output = tmp_path / "catalog.parquet"
        result = build_catalog("2024-08-22", "2024-08-22", output)

        assert result == output
        assert output.exists()
        df = pd.read_parquet(output)
        assert len(df) == 2
        assert list(df.columns) == ["date", "h", "v", "task_id", "source_uri"]

    @patch("atlantis.fetchers.modis.catalog.earthdata_auth_headers")
    @patch("atlantis.fetchers.modis.catalog._list_tiles_for_date")
    def test_raises_when_no_tiles_found(self, mock_list, mock_auth, tmp_path):
        mock_auth.return_value = {"Authorization": "Bearer token"}
        mock_list.return_value = []
        with pytest.raises(RuntimeError, match="No MODIS tiles found"):
            build_catalog("2024-08-22", "2024-08-22", tmp_path / "catalog.parquet")

    @patch("atlantis.fetchers.modis.catalog.earthdata_auth_headers")
    @patch("atlantis.fetchers.modis.catalog._list_tiles_for_date")
    def test_continues_on_request_exception(self, mock_list, mock_auth, tmp_path):
        """A failed date listing should be skipped, not crash the whole run."""
        mock_auth.return_value = {"Authorization": "Bearer token"}
        mock_list.side_effect = [
            requests.RequestException("network error"),
            [
                {
                    "date": "2024-08-23",
                    "h": 24,
                    "v": 5,
                    "task_id": "modis-20240823-h24v05",
                    "source_uri": "https://example.com/h24v05.hdf",
                }
            ],
        ]
        output = tmp_path / "catalog.parquet"
        result = build_catalog("2024-08-22", "2024-08-23", output)

        assert result == output
        df = pd.read_parquet(output)
        assert len(df) == 1

    @patch("atlantis.fetchers.modis.catalog.earthdata_auth_headers")
    @patch("atlantis.fetchers.modis.catalog._list_tiles_for_date")
    def test_progress_callback_invoked(self, mock_list, mock_auth, tmp_path):
        mock_auth.return_value = {"Authorization": "Bearer token"}
        mock_list.return_value = [
            {
                "date": "2024-08-22",
                "h": 24,
                "v": 5,
                "task_id": "modis-20240822-h24v05",
                "source_uri": "https://example.com/h24v05.hdf",
            },
        ]
        messages = []
        build_catalog("2024-08-22", "2024-08-22", tmp_path / "catalog.parquet", on_progress=messages.append)
        assert len(messages) == 1
        assert "MODIS catalog" in messages[0]
