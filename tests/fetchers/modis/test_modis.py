"""Tests for the MODIS fetcher (integration / orchestration)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import rasterio
import requests
from rasterio.transform import from_origin

from atlantis.fetchers.base import FetchResult, SearchResult
from atlantis.fetchers.modis import (
    MODISFetcher,
    _normalise_backend,
    _normalise_composite,
)
from atlantis.fetchers.modis.backend import (
    LaadsHdf4Backend,
    ModisListingEntry,
)
from atlantis.fetchers.modis.processor import (
    ModisRasterProcessor,
    ProcessedTile,
    ProcessTilesResult,
)
from atlantis.models.event import FloodEvent
from atlantis.models.metadata import TileMetadata

# ── Helper builders ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _set_token(monkeypatch):
    monkeypatch.setenv("EARTHDATA_TOKEN", "test-token")


@pytest.fixture(autouse=True)
def _bypass_hdf4_check(monkeypatch):
    # Skip the GDAL HDF4 driver presence check for unit tests.
    monkeypatch.setattr(LaadsHdf4Backend, "_verify_hdf4_driver", staticmethod(lambda: None))


@pytest.fixture(autouse=True)
def _reset_config_cache():
    from atlantis.config import reload_config

    reload_config()
    yield
    reload_config()


def _make_event(
    *,
    start: date = date(2026, 6, 1),
    end: date | None = None,
    bbox: tuple[float, float, float, float] = (66.0, 22.0, 72.0, 31.0),
) -> FloodEvent:
    return FloodEvent(
        event_id="test_event",
        bbox=bbox,
        start_date=start,
        end_date=end or start,
    )


def _make_processed_tile(*, flood_count: int = 0) -> ProcessedTile:
    """Build a 4×4 ProcessedTile with the requested number of flood pixels."""
    flood = np.zeros((4, 4), dtype=np.float32)
    if flood_count:
        flat = flood.reshape(-1)
        flat[:flood_count] = 1.0
    return ProcessedTile(
        transform=from_origin(0.0, 1.0, 0.25, 0.25),
        crs="EPSG:4326",
        cloud_fraction=0.0,
        flood_fraction=flood,
        recurring_flood=np.zeros((4, 4), dtype=np.uint8),
        permanent_water=np.zeros((4, 4), dtype=np.uint8),
        quality_mask=np.ones((4, 4), dtype=np.uint8),
    )


def _make_process_result(*, flood_count: int = 0) -> ProcessTilesResult:
    proc = _make_processed_tile(flood_count=flood_count)
    metadata = TileMetadata(
        event_id="test_event",
        source_id="modis",
        fetch_timestamp=datetime.now(timezone.utc),
        crs="EPSG:4326",
        resolution=0.25,
        bbox=(0.0, 0.0, 1.0, 1.0),
        cloud_fraction=0.0,
        quality_bitmask=0,
        permanent_water_mask_available=True,
    )
    from atlantis.fetchers.modis.processor import OutputPaths

    return ProcessTilesResult(paths=OutputPaths(), metadata=metadata, processed=proc)


# ── Helper-function tests ────────────────────────────────────────────────


class TestNormaliseBackend:
    def test_valid(self):
        assert _normalise_backend("lance_geotiff") == "lance_geotiff"
        assert _normalise_backend("laads_hdf4") == "laads_hdf4"

    def test_case_insensitive(self):
        assert _normalise_backend("LANCE_GEOTIFF") == "lance_geotiff"

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Unsupported MODIS backend"):
            _normalise_backend("nope")


class TestNormaliseComposite:
    @pytest.mark.parametrize("comp", ["F1", "F1C", "F2", "F3"])
    def test_valid(self, comp):
        assert _normalise_composite(comp) == comp

    def test_lowercase_normalised(self):
        assert _normalise_composite("f2") == "F2"

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="composite"):
            _normalise_composite("FX")


# ── MODISFetcher constructor ─────────────────────────────────────────────


class TestFetcherInit:
    def test_defaults(self):
        f = MODISFetcher()
        assert f.backend_name == "lance_geotiff"
        assert f.composite == "F2"
        assert f.strategy == "peak"
        assert f.classify is False
        assert f.stream is False

    def test_invalid_strategy_raises(self):
        with pytest.raises(ValueError, match="Invalid strategy"):
            MODISFetcher(strategy="bogus")

    def test_stream_with_laads_hdf4_raises(self):
        with pytest.raises(ValueError, match="does not support --stream"):
            MODISFetcher(backend="laads_hdf4", stream=True)

    def test_stream_with_lance_ok(self):
        f = MODISFetcher(backend="lance_geotiff", stream=True)
        assert f.stream is True


# ── search() ────────────────────────────────────────────────────────────


class TestSearch:
    def test_returns_empty_when_token_missing(self, monkeypatch):
        monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
        f = MODISFetcher()
        results = f.search(_make_event())
        assert results == []
        assert f.last_diagnostics is not None
        assert f.last_diagnostics.auth_token_missing

    def test_returns_empty_when_no_tiles(self, monkeypatch):
        # Force modis_tiles_for_bbox to return [] (degenerate AOI).
        f = MODISFetcher()
        with patch("atlantis.fetchers.modis.modis_tiles_for_bbox", return_value=[]):
            results = f.search(_make_event())
        assert results == []

    def test_search_yields_per_tile_results(self):
        f = MODISFetcher(backend="lance_geotiff", composite="F2")
        # Patch the LANCE backend listing path to produce one matching entry per tile.
        entries = [
            ModisListingEntry(
                filename="MCDWD_L3_F2_NRT.A2026152.h25v05.061.2026152120000.tif",
                url="https://example/MCDWD_L3_F2_NRT.A2026152.h25v05.061.2026152120000.tif",
                prod_timestamp="2026152120000",
            ),
            ModisListingEntry(
                filename="MCDWD_L3_F2_NRT.A2026152.h25v06.061.2026152120000.tif",
                url="https://example/MCDWD_L3_F2_NRT.A2026152.h25v06.061.2026152120000.tif",
                prod_timestamp="2026152120000",
            ),
            ModisListingEntry(
                filename="MCDWD_L3_F2_NRT.A2026152.h26v05.061.2026152120000.tif",
                url="https://example/MCDWD_L3_F2_NRT.A2026152.h26v05.061.2026152120000.tif",
                prod_timestamp="2026152120000",
            ),
            ModisListingEntry(
                filename="MCDWD_L3_F2_NRT.A2026152.h26v06.061.2026152120000.tif",
                url="https://example/MCDWD_L3_F2_NRT.A2026152.h26v06.061.2026152120000.tif",
                prod_timestamp="2026152120000",
            ),
        ]
        # Use a date inside the LANCE retention window (within last 7 days).
        recent = datetime.now(timezone.utc).date()
        event = _make_event(start=recent, end=recent)

        with patch.object(f.backend, "get_directory_listing", return_value=entries):
            results = f.search(event)

        # Pakistan-style bbox (66, 22, 72, 31) maps to tiles (24..25, 5..6).
        # Our fixture covers (25,*) and (26,*); only (25,5) and (25,6) match.
        assert len(results) == 2
        assert all(r.source_id == "modis" for r in results)
        item_ids = {r.item_id for r in results}
        assert all(":h25v" in iid for iid in item_ids)

    def test_outside_lance_window_flag(self, monkeypatch):
        # Date well in the past — should be marked outside_lance_window.
        f = MODISFetcher(backend="lance_geotiff")
        event = _make_event(start=date(2010, 1, 1), end=date(2010, 1, 1))
        with patch.object(f.backend, "get_directory_listing", return_value=[]):
            results = f.search(event)
        assert results == []
        assert f.last_diagnostics.outside_lance_window


# ── fetch() — strategy dispatch with mocked processor ───────────────────


class TestFetchStrategyDispatch:
    """Drive fetch() through search/processor mocks to test all three strategies."""

    @pytest.fixture
    def fake_search_results(self):
        ts = datetime(2026, 6, 1, tzinfo=timezone.utc)

        def _result(date_token: str, h: int, v: int) -> SearchResult:
            return SearchResult(
                source_id="modis",
                item_id=f"modis:{date_token}:h{h:02d}v{v:02d}",
                timestamp=ts,
                bbox=(0.0, 0.0, 1.0, 1.0),
                url=f"https://example/{date_token}_h{h:02d}v{v:02d}.tif",
                properties={
                    "h": h,
                    "v": v,
                    "date": date_token,
                    "filename": f"{date_token}_h{h:02d}v{v:02d}.tif",
                    "prod_timestamp": None,
                    "backend": "lance_geotiff",
                    "composite": "F2",
                },
            )

        return [
            _result("20260601", 25, 5),
            _result("20260602", 25, 5),
        ]

    def test_peak_strategy_picks_highest_flood(self, tmp_path, fake_search_results):
        f = MODISFetcher(backend="lance_geotiff", strategy="peak", keep_processed=False, stream=True)

        date1 = _make_process_result(flood_count=2)
        date2 = _make_process_result(flood_count=10)  # should win

        with (
            patch.object(MODISFetcher, "search", return_value=fake_search_results),
            patch.object(
                ModisRasterProcessor,
                "process_tiles",
                side_effect=[date1, date2],
            ),
        ):
            results = f.fetch(_make_event(), tmp_path)

        assert len(results) == 1
        assert results[0].date_token == "20260602"

    def test_aggregate_strategy_returns_single_aggregated(self, tmp_path, fake_search_results):
        f = MODISFetcher(backend="lance_geotiff", strategy="aggregate", keep_processed=False, stream=True)
        date1 = _make_process_result(flood_count=2)
        date2 = _make_process_result(flood_count=10)

        with (
            patch.object(MODISFetcher, "search", return_value=fake_search_results),
            patch.object(
                ModisRasterProcessor,
                "process_tiles",
                side_effect=[date1, date2],
            ),
        ):
            results = f.fetch(_make_event(), tmp_path)

        assert len(results) == 1
        assert results[0].date_token == "aggregated"

    def test_all_strategy_returns_per_date(self, tmp_path, fake_search_results):
        f = MODISFetcher(backend="lance_geotiff", strategy="all", keep_processed=False, stream=True)
        date1 = _make_process_result(flood_count=2)
        date2 = _make_process_result(flood_count=10)

        with (
            patch.object(MODISFetcher, "search", return_value=fake_search_results),
            patch.object(
                ModisRasterProcessor,
                "process_tiles",
                side_effect=[date1, date2],
            ),
        ):
            results = f.fetch(_make_event(), tmp_path)

        assert len(results) == 2
        date_tokens = {r.date_token for r in results}
        assert date_tokens == {"20260601", "20260602"}

    def test_keep_processed_writes_only_survivors(self, tmp_path, fake_search_results):
        """#4: processed/ is written only for dates surviving the peak window.

        Two dates are processed in-memory; ``max_observations=1`` prunes to the
        peak date before any processed GeoTIFF is persisted, so
        ``write_processed`` must be invoked exactly once (mirrors VIIRS).
        """
        f = MODISFetcher(
            backend="lance_geotiff",
            strategy="all",
            keep_processed=True,
            max_observations=1,
            stream=True,
        )
        date1 = _make_process_result(flood_count=2)
        date2 = _make_process_result(flood_count=10)  # peak — should survive

        write_spy = patch.object(ModisRasterProcessor, "write_processed", MagicMock())
        with (
            patch.object(MODISFetcher, "search", return_value=fake_search_results),
            patch.object(
                ModisRasterProcessor,
                "process_tiles",
                side_effect=[date1, date2],
            ),
            write_spy as spy,
        ):
            f.fetch(_make_event(), tmp_path)

        assert spy.call_count == 1


class TestFetchStreamingEnv:
    def test_stream_passes_bearer_to_gdal(self, tmp_path):
        f = MODISFetcher(backend="lance_geotiff", stream=True, strategy="peak", keep_processed=False)
        ts = datetime(2026, 6, 1, tzinfo=timezone.utc)
        sr = SearchResult(
            source_id="modis",
            item_id="modis:20260601:h25v05",
            timestamp=ts,
            bbox=(0, 0, 1, 1),
            url="https://example/MCDWD_L3_F2_NRT.A2026152.h25v05.061.0.tif",
            properties={
                "h": 25,
                "v": 5,
                "date": "20260601",
                "filename": "MCDWD_L3_F2_NRT.A2026152.h25v05.061.0.tif",
                "prod_timestamp": None,
                "backend": "lance_geotiff",
                "composite": "F2",
            },
        )

        captured: dict[str, str] = {}

        original_env = rasterio.Env

        def _spy_env(**kwargs):
            captured.update(kwargs)
            return original_env(**kwargs)

        with (
            patch.object(MODISFetcher, "search", return_value=[sr]),
            patch.object(
                ModisRasterProcessor,
                "process_tiles",
                return_value=_make_process_result(flood_count=1),
            ),
            patch("atlantis.fetchers.modis.rasterio.Env", side_effect=_spy_env),
        ):
            results = f.fetch(_make_event(), tmp_path)

        assert len(results) == 1
        assert "GDAL_HTTP_HEADERS" in captured
        assert captured["GDAL_HTTP_HEADERS"] == "Authorization: Bearer test-token"


class TestFetchEmptySearch:
    def test_empty_search_returns_empty(self, tmp_path):
        f = MODISFetcher()
        with patch.object(MODISFetcher, "search", return_value=[]):
            results = f.fetch(_make_event(), tmp_path)
        assert results == []


class TestToDataset:
    def test_stored_flood_nodata_round_trips_to_nan(self, tmp_path):
        fetcher = MODISFetcher(classify=True)

        flood = np.array([[100, 255], [0, 255]], dtype=np.uint8)
        quality = np.array([[1, 0], [1, 0]], dtype=np.uint8)
        permanent_water = np.array([[0, 0], [1, 0]], dtype=np.uint8)
        recurring = np.array([[0, 0], [0, 0]], dtype=np.uint8)

        def _write_layer(path, data, *, nodata):
            with rasterio.open(
                path,
                "w",
                driver="GTiff",
                height=2,
                width=2,
                count=1,
                dtype="uint8",
                crs="EPSG:4326",
                transform=from_origin(20.0, 31.0, 0.5, 0.5),
                nodata=nodata,
            ) as dst:
                dst.write(data, 1)

        ff_path = tmp_path / "test_flood_fraction.tif"
        qm_path = tmp_path / "test_quality_mask.tif"
        pw_path = tmp_path / "test_permanent_water.tif"
        rf_path = tmp_path / "test_recurring_flood.tif"

        _write_layer(ff_path, flood, nodata=255)
        _write_layer(qm_path, quality, nodata=0)
        _write_layer(pw_path, permanent_water, nodata=0)
        _write_layer(rf_path, recurring, nodata=0)

        result = FetchResult(
            event_id="test_event",
            source_id="modis",
            files=[ff_path, qm_path, pw_path, rf_path],
            metadata=TileMetadata(
                event_id="test_event",
                source_id="modis",
                fetch_timestamp=datetime(2020, 7, 22, tzinfo=timezone.utc),
                bbox=(20.0, 30.0, 21.0, 31.0),
            ),
        )

        dataset = fetcher.to_dataset(result)

        flood_fraction = dataset["flood_fraction"].values
        assert flood_fraction[0, 0] == pytest.approx(1.0, rel=1e-6)
        assert flood_fraction[1, 0] == pytest.approx(0.0, rel=1e-6)
        assert np.isnan(flood_fraction[0, 1])
        assert np.isnan(flood_fraction[1, 1])


class TestLaadsDownloadRetry:
    @staticmethod
    def _http_error(status_code: int) -> requests.HTTPError:
        response = requests.Response()
        response.status_code = status_code
        response.url = "https://example.test/file.hdf"
        return requests.HTTPError(f"{status_code} error", response=response)

    @staticmethod
    def _laads_result() -> SearchResult:
        return SearchResult(
            source_id="modis",
            item_id="modis:20201013:h17v07",
            timestamp=datetime(2020, 10, 13, tzinfo=timezone.utc),
            bbox=(0.0, 0.0, 1.0, 1.0),
            url="https://example.test/MCDWD_L3.A2020287.h17v07.061.1234567890123.hdf",
            properties={
                "h": 17,
                "v": 7,
                "date": "20201013",
                "filename": "MCDWD_L3.A2020287.h17v07.061.1234567890123.hdf",
                "prod_timestamp": "1234567890123",
                "backend": "laads_hdf4",
                "composite": "F2",
            },
        )

    def test_retries_transient_401_then_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLANTIS_MAX_RETRIES", "2")
        from atlantis.config import reload_config

        reload_config()
        f = MODISFetcher(backend="laads_hdf4", keep_processed=False, strategy="all")

        with (
            patch.object(MODISFetcher, "search", return_value=[self._laads_result()]),
            patch(
                "atlantis.fetchers.modis.download_file",
                side_effect=[self._http_error(401), "dummy.hdf"],
            ) as download_mock,
            patch("atlantis.fetchers.modis.time_module.sleep") as sleep_mock,
            patch.object(
                ModisRasterProcessor,
                "process_tiles",
                return_value=_make_process_result(flood_count=1),
            ),
        ):
            results = f.fetch(_make_event(start=date(2020, 10, 13)), tmp_path)

        assert len(results) == 1
        assert download_mock.call_count == 2
        sleep_mock.assert_called_once()

    def test_does_not_retry_non_retryable_status(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLANTIS_MAX_RETRIES", "3")
        from atlantis.config import reload_config

        reload_config()
        f = MODISFetcher(backend="laads_hdf4", keep_processed=False, strategy="all")

        with (
            patch.object(MODISFetcher, "search", return_value=[self._laads_result()]),
            patch(
                "atlantis.fetchers.modis.download_file",
                side_effect=self._http_error(404),
            ) as download_mock,
            patch("atlantis.fetchers.modis.time_module.sleep") as sleep_mock,
        ):
            with pytest.raises(requests.HTTPError):
                f.fetch(_make_event(start=date(2020, 10, 13)), tmp_path)

        assert download_mock.call_count == 1
        sleep_mock.assert_not_called()
