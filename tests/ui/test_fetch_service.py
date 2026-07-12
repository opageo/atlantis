"""Tests for the fetch service adapter layer."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from atlantis.ui.models import FetchRequest
from atlantis.ui.services.fetch_service import (
    _build_fetcher_kwargs,
    _date_label,
    _ds_is_classified,
    _first_geotiff,
    _parse_bbox,
    _select_best_result,
)


class TestParseBbox:
    """Tests for bbox string parsing."""

    def test_parses_space_separated(self) -> None:
        """Four space-separated floats."""
        assert _parse_bbox("-1.5 38.8 0.5 40.0") == (-1.5, 38.8, 0.5, 40.0)

    def test_parses_comma_separated(self) -> None:
        """Commas are treated as delimiters."""
        assert _parse_bbox("-1.5,38.8,0.5,40.0") == (-1.5, 38.8, 0.5, 40.0)

    def test_parses_mixed_delimiters(self) -> None:
        """Mixed commas and spaces."""
        assert _parse_bbox("-1.5, 38.8 0.5, 40.0") == (-1.5, 38.8, 0.5, 40.0)

    def test_parses_integers(self) -> None:
        """Integers are accepted and cast to float."""
        assert _parse_bbox("0 0 10 10") == (0.0, 0.0, 10.0, 10.0)

    def test_parses_negative_numbers(self) -> None:
        """Negative coordinates work."""
        assert _parse_bbox("-180 -90 180 90") == (-180.0, -90.0, 180.0, 90.0)

    def test_too_few_parts_raises(self) -> None:
        """Fewer than 4 numbers raises ValueError."""
        with pytest.raises(ValueError, match="exactly four numbers"):
            _parse_bbox("1 2 3")

    def test_too_many_parts_raises(self) -> None:
        """More than 4 numbers raises ValueError."""
        with pytest.raises(ValueError, match="exactly four numbers"):
            _parse_bbox("1 2 3 4 5")

    def test_empty_string_raises(self) -> None:
        """Empty string raises ValueError."""
        with pytest.raises(ValueError, match="exactly four numbers"):
            _parse_bbox("")

    def test_non_numeric_raises(self) -> None:
        """Non-numeric tokens raise ValueError."""
        with pytest.raises(ValueError):
            _parse_bbox("a b c d")


class TestDateLabel:
    """Tests for extracting date labels from fetch results."""

    def test_from_timestamp(self) -> None:
        """Uses timestamp attribute when present."""
        result = MagicMock(timestamp=date(2024, 10, 29), date_token=None)
        assert _date_label(result) == "20241029"

    def test_from_date_token(self) -> None:
        """Falls back to date_token attribute."""
        result = MagicMock(timestamp=None, date_token="A2024300")
        assert _date_label(result) == "A2024300"

    def test_timestamp_overrides_date_token(self) -> None:
        """Timestamp takes priority over date_token."""
        result = MagicMock(timestamp=date(2024, 1, 1), date_token="X")
        assert _date_label(result) == "20240101"

    def test_unknown_when_no_attributes(self) -> None:
        """Returns 'unknown' when neither attribute is set."""
        result = MagicMock(spec=[])  # no timestamp, no date_token
        assert _date_label(result) == "unknown"

    def test_date_token_when_timestamp_is_none(self) -> None:
        """If timestamp is None, uses date_token."""
        result = MagicMock(timestamp=None, date_token="2024-01", spec=["timestamp", "date_token"])
        assert _date_label(result) == "2024-01"


class TestDsIsClassified:
    """Tests for classified dataset detection."""

    def test_classified_ds_has_flood_fraction(self) -> None:
        """Dataset with 'flood_fraction' key is classified."""
        ds = {"flood_fraction": MagicMock()}
        assert _ds_is_classified(ds, "viirs") is True

    def test_raw_ds_is_not_classified(self) -> None:
        """Dataset without 'flood_fraction' is not classified."""
        ds = {"raw": MagicMock()}
        assert _ds_is_classified(ds, "viirs") is False

    def test_empty_dataset_is_not_classified(self) -> None:
        """Empty dict is not classified."""
        assert _ds_is_classified({}, "viirs") is False

    def test_source_id_is_ignored(self) -> None:
        """The source_id parameter does not affect the result."""
        ds = {"flood_fraction": MagicMock()}
        assert _ds_is_classified(ds, "gfm") is True
        assert _ds_is_classified(ds, "modis") is True


class TestFirstGeotiff:
    """Tests for locating the first GeoTIFF in a file list."""

    def test_finds_tif(self, tmp_path: Path) -> None:
        """Returns the first .tif file."""
        t1 = tmp_path / "a.tif"
        t2 = tmp_path / "b.tif"
        t1.write_text("")
        t2.write_text("")
        assert _first_geotiff([t1, t2], None) == t1

    def test_finds_tiff(self, tmp_path: Path) -> None:
        """Returns the first .tiff file."""
        t = tmp_path / "img.tiff"
        t.write_text("")
        assert _first_geotiff([t], None) == t

    def test_prefers_geotiff_over_other(self, tmp_path: Path) -> None:
        """Skips non-GeoTIFF files."""
        txt = tmp_path / "readme.txt"
        tif = tmp_path / "data.tif"
        txt.write_text("")
        tif.write_text("")
        assert _first_geotiff([txt, tif], None) == tif

    def test_no_geotiff_falls_back_to_first(self, tmp_path: Path) -> None:
        """If no GeoTIFF found, returns the first file."""
        txt = tmp_path / "a.txt"
        txt.write_text("")
        assert _first_geotiff([txt], None) == txt

    def test_empty_list_returns_none(self) -> None:
        """Empty file list returns None."""
        assert _first_geotiff([], None) is None


class TestBuildFetcherKwargs:
    """Tests for source-specific kwargs builder."""

    def test_viirs_defaults(self) -> None:
        """VIIRS builds with backend, format, and stream."""
        req = FetchRequest(
            event_id="v",
            bbox="0 0 1 1",
            start_date="2024-01-01",
            end_date="2024-01-02",
            source="viirs",
        )
        kw = _build_fetcher_kwargs(req)
        assert kw["classify"] is True
        assert kw["strategy"] == "peak"
        assert kw["keep_processed"] is True
        assert kw["backend"] == "noaa_s3"
        assert kw["data_format"] == "tif"
        assert kw["stream"] is True

    def test_modis_defaults(self) -> None:
        """MODIS builds with backend, composite, and stream."""
        req = FetchRequest(
            event_id="m",
            bbox="0 0 1 1",
            start_date="2024-01-01",
            end_date="2024-01-02",
            source="modis",
        )
        kw = _build_fetcher_kwargs(req)
        assert kw["backend"] == "lance_geotiff"
        assert kw["composite"] == "F2"
        assert kw["stream"] is True  # stream + lance_geotiff

    def test_modis_stream_false_with_laads(self) -> None:
        """Stream is disabled when backend is laads_hdf4."""
        req = FetchRequest(
            event_id="m",
            bbox="0 0 1 1",
            start_date="2024-01-01",
            end_date="2024-01-02",
            source="modis",
            modis_backend="laads_hdf4",
        )
        kw = _build_fetcher_kwargs(req)
        assert kw["backend"] == "laads_hdf4"
        assert kw["stream"] is False

    def test_modis_stream_false_when_flag_off(self) -> None:
        """Stream is disabled when user toggles it off."""
        req = FetchRequest(
            event_id="m",
            bbox="0 0 1 1",
            start_date="2024-01-01",
            end_date="2024-01-02",
            source="modis",
            stream=False,
        )
        kw = _build_fetcher_kwargs(req)
        assert kw["stream"] is False

    def test_gfm_defaults(self) -> None:
        """GFM builds with coarsen factor and resampling."""
        from rasterio.enums import Resampling

        req = FetchRequest(
            event_id="g",
            bbox="0 0 1 1",
            start_date="2024-01-01",
            end_date="2024-01-02",
            source="gfm",
        )
        kw = _build_fetcher_kwargs(req)
        assert kw["coarsen_factor"] == 4
        assert kw["resampling"] == Resampling.average

    def test_gfm_custom_resampling(self) -> None:
        """Custom resampling string is mapped to Resampling enum."""
        from rasterio.enums import Resampling

        req = FetchRequest(
            event_id="g",
            bbox="0 0 1 1",
            start_date="2024-01-01",
            end_date="2024-01-02",
            source="gfm",
            gfm_resampling="nearest",
        )
        kw = _build_fetcher_kwargs(req)
        assert kw["resampling"] == Resampling.nearest

    def test_gfm_invalid_resampling_falls_back(self) -> None:
        """Invalid resampling name falls back to average."""
        from rasterio.enums import Resampling

        req = FetchRequest(
            event_id="g",
            bbox="0 0 1 1",
            start_date="2024-01-01",
            end_date="2024-01-02",
            source="gfm",
            gfm_resampling="unknown_method",
        )
        kw = _build_fetcher_kwargs(req)
        assert kw["resampling"] == Resampling.average

    def test_custom_strategy(self) -> None:
        """Strategy is passed through."""
        req = FetchRequest(
            event_id="s",
            bbox="0 0 1 1",
            start_date="2024-01-01",
            end_date="2024-01-02",
            source="viirs",
            strategy="aggregate",
        )
        kw = _build_fetcher_kwargs(req)
        assert kw["strategy"] == "aggregate"

    def test_classify_false(self) -> None:
        """classify=False is passed through."""
        req = FetchRequest(
            event_id="c",
            bbox="0 0 1 1",
            start_date="2024-01-01",
            end_date="2024-01-02",
            source="viirs",
            classify=False,
        )
        kw = _build_fetcher_kwargs(req)
        assert kw["classify"] is False


class TestSelectBestResult:
    """Tests for peak-flood result selection."""

    def test_single_result_returned_directly(self) -> None:
        """A single fetch result is selected immediately."""
        fetcher = MagicMock()
        fetcher.source_id = "viirs"
        raw_da = MagicMock()
        raw_da.values = np.array([])
        ds = {"raw": raw_da}
        fetcher.to_dataset.return_value = ds
        result = MagicMock(timestamp=date(2024, 10, 29))
        best, label = _select_best_result(fetcher, [result])
        assert best is result
        assert label == "20241029"

    def test_picks_highest_flood_count_from_classified(self) -> None:
        """Among classified datasets, the one with most flooded pixels wins."""
        fetcher = MagicMock()
        fetcher.source_id = "viirs"
        r1 = MagicMock(timestamp=date(2024, 10, 29))
        r2 = MagicMock(timestamp=date(2024, 10, 30))

        ds1 = {"flood_fraction": MagicMock()}
        ds1["flood_fraction"].values = np.array([0.0, 0.5, 0.0])
        ds2 = {"flood_fraction": MagicMock()}
        ds2["flood_fraction"].values = np.array([1.0, 1.0, 0.0])

        fetcher.to_dataset.side_effect = [ds1, ds2]
        best, label = _select_best_result(fetcher, [r1, r2])
        assert best is r2
        assert label == "20241030"

    def test_no_classified_falls_back_to_first(self) -> None:
        """When no result is classified, the first result is used."""
        fetcher = MagicMock()
        fetcher.source_id = "viirs"
        r1 = MagicMock(timestamp=date(2024, 1, 1))
        r2 = MagicMock(timestamp=date(2024, 1, 2))

        raw_da = MagicMock()
        raw_da.values = np.zeros(3)
        ds1 = {"raw": raw_da}
        ds2 = {"raw": raw_da}
        fetcher.to_dataset.side_effect = [ds1, ds2]

        best, label = _select_best_result(fetcher, [r1, r2])
        assert best is r1
        assert label == "20240101"

    def test_uses_date_token_fallback(self) -> None:
        """When timestamp is absent, date_token is used as label."""
        fetcher = MagicMock()
        fetcher.source_id = "viirs"
        r1 = MagicMock(timestamp=None, date_token="A2024001")
        raw_da = MagicMock()
        raw_da.values = np.array([])
        fetcher.to_dataset.return_value = {"raw": raw_da}
        best, label = _select_best_result(fetcher, [r1])
        assert label == "A2024001"

    def test_empty_list_fallback(self) -> None:
        """Single result with no classification picks first."""
        fetcher = MagicMock()
        fetcher.source_id = "viirs"
        r = MagicMock(timestamp=date(2024, 6, 15))
        raw_da = MagicMock()
        raw_da.values = np.array([])
        fetcher.to_dataset.return_value = {"raw": raw_da}
        best, label = _select_best_result(fetcher, [r])
        assert best is r


class TestRunFetchErrorPaths:
    """Tests for error-handling paths in run_fetch."""

    def test_invalid_bbox_returns_error(self) -> None:
        """An unparseable bbox yields an error FetchResponse immediately."""
        import asyncio

        from atlantis.ui.services.fetch_service import run_fetch

        req = FetchRequest(
            event_id="bad",
            bbox="1 2",
            start_date="2024-01-01",
            end_date="2024-01-02",
            source="viirs",
        )

        calls = []

        async def _run():
            resp = await run_fetch(req, lambda p: calls.append(p))
            return resp

        resp = asyncio.new_event_loop().run_until_complete(_run())
        assert resp.error is not None
        assert "exactly four numbers" in resp.error

    def test_unknown_source_returns_error(self) -> None:
        """An unrecognized source id yields an error FetchResponse."""
        import asyncio

        from atlantis.ui.services.fetch_service import run_fetch

        with patch("atlantis.ui.services.fetch_service.get_config") as mock_cfg:
            mock_cfg.return_value = MagicMock()
            mock_cfg.return_value.fetcher.cache_dir = Path("/tmp")

            req = FetchRequest(
                event_id="ev",
                bbox="0 0 1 1",
                start_date="2024-01-01",
                end_date="2024-01-02",
                source="nonexistent",
            )

            async def _run():
                return await run_fetch(req, lambda p: None)

            resp = asyncio.new_event_loop().run_until_complete(_run())
            assert resp.error is not None
            assert "Unknown source" in resp.error

    def test_invalid_date_returns_error(self) -> None:
        """An invalid date string yields an error FetchResponse."""
        import asyncio

        from atlantis.ui.services.fetch_service import run_fetch

        with patch("atlantis.ui.services.fetch_service.get_config") as mock_cfg:
            mock_cfg.return_value = MagicMock()
            mock_cfg.return_value.fetcher.cache_dir = Path("/tmp")

            req = FetchRequest(
                event_id="ev",
                bbox="0 0 1 1",
                start_date="not-a-date",
                end_date="2024-01-02",
                source="viirs",
            )

            async def _run():
                return await run_fetch(req, lambda p: None)

            resp = asyncio.new_event_loop().run_until_complete(_run())
            assert resp.error is not None


class TestPlotSource:
    """Tests for the _plot_source function."""

    def test_classified_viirs_plot(self, tmp_path: Path) -> None:
        """When ds has flood_fraction, plot_classified is called."""
        from atlantis.ui.services.fetch_service import _plot_source

        flood_da = MagicMock()
        ds = {"flood_fraction": flood_da}
        png = tmp_path / "plot.png"

        with patch("atlantis.ui.services.fetch_service.plot_classified") as mock_plot:
            _plot_source(ds, "Valencia", "20241029", source_id="viirs", output_png_path=png)
            mock_plot.assert_called_once()

    def test_gfm_ensemble_plot(self, tmp_path: Path) -> None:
        """When ds has ensemble_flood_extent (and not flood_fraction), plot_raw is called."""
        from atlantis.ui.services.fetch_service import _plot_source

        ds = {"ensemble_flood_extent": MagicMock()}
        png = tmp_path / "plot.png"

        with patch("atlantis.ui.services.fetch_service.plot_raw") as mock_plot:
            _plot_source(ds, "GFM_Event", "20241029", source_id="gfm", output_png_path=png)
            mock_plot.assert_called_once()

    def test_raw_modis_plot(self, tmp_path: Path) -> None:
        """When ds has only 'raw', plot_raw is called with MODIS codes."""
        from atlantis.ui.services.fetch_service import _plot_source

        ds = {"raw": MagicMock()}
        png = tmp_path / "plot.png"

        with patch("atlantis.ui.services.fetch_service.plot_raw") as mock_plot:
            _plot_source(ds, "ModEvent", "20241029", source_id="modis", output_png_path=png)
            mock_plot.assert_called_once()

    def test_raw_viirs_plot(self, tmp_path: Path) -> None:
        """Raw VIIRS data uses VIIRS codes and filters pixel range."""
        from atlantis.ui.services.fetch_service import _plot_source

        raw_da = MagicMock()
        raw_da.__lt__ = MagicMock(return_value=True)
        raw_da.__gt__ = MagicMock(return_value=False)
        raw_da.__or__ = MagicMock(return_value=True)
        filtered = MagicMock()
        raw_da.where.return_value = filtered
        ds = {"raw": raw_da}
        png = tmp_path / "plot.png"

        with patch("atlantis.ui.services.fetch_service.plot_raw") as mock_plot:
            _plot_source(ds, "ViirsEvent", "20241029", source_id="viirs", output_png_path=png)
            mock_plot.assert_called_once()
