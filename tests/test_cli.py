"""Tests for CLI commands."""

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
from typer.testing import CliRunner

from atlantis import __version__
from atlantis.cli import _plot_viirs, _select_best_result, _should_emit_verbose_log, cli
from atlantis.fetchers.base import FetchResult
from atlantis.models.event import FloodEvent
from atlantis.models.metadata import TileMetadata
from atlantis.utils.checklist import task_checklist

runner = CliRunner()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_metadata(event_id: str, bbox: tuple[float, float, float, float]) -> TileMetadata:
    return TileMetadata(
        event_id=event_id,
        source_id="viirs",
        fetch_timestamp=datetime.now(timezone.utc),
        bbox=bbox,
    )


class _FakeDataArray:
    """Minimal stand-in for ``xr.DataArray`` that preserves ``.values``."""

    def __init__(self, arr: np.ndarray | None = None):
        self._arr = arr if arr is not None else np.zeros((4, 4), dtype=np.float32)

    @property
    def values(self) -> np.ndarray:
        return self._arr

    def max(self):
        return self._arr.max()


class _FakeDataset:
    """Minimal stand-in for ``xr.DataArray`` / dict-like Dataset.

    Supports ``"var" in ds``, ``ds["var"].values``, and iteration.
    """

    def __init__(self, variables: dict[str, np.ndarray] | None = None):
        if variables is None:
            variables = {":": np.zeros((4, 4), dtype=np.float32)}
        self._vars = {k: _FakeDataArray(v) for k, v in variables.items()}

    def __contains__(self, key):
        return key in self._vars

    def __getitem__(self, key):
        return self._vars[key]

    def __iter__(self):
        return iter(self._vars)

    @property
    def data_vars(self):
        return list(self._vars.keys())


def _dummy_fetch_result(
    event_id: str,
    tmp_path: Path,
    *,
    date_token: str = "20200722",
    flood_pixels: int = 0,
) -> tuple[FetchResult, _FakeDataset]:
    """Create a FetchResult + matching FakeDataset for testing."""
    bbox = (105.0, 28.0, 125.0, 38.0)
    tif_path = tmp_path / f"{event_id}_{date_token}_viirs_flood_fraction.tif"
    fetch_result = FetchResult(
        event_id=event_id,
        source_id="viirs",
        files=[tif_path],
        metadata=_make_metadata(event_id, bbox),
    )
    # Build a flood_extent array: first `flood_pixels` entries are 1.0, rest 0.0
    arr = np.zeros((4, 4), dtype=np.float32)
    flat = arr.ravel()
    flat[: min(flood_pixels, flat.size)] = 1.0
    ds = _FakeDataset({"flood_fraction": arr})
    return fetch_result, ds


def test_version():
    """Test version is correctly set."""
    assert __version__ == "0.1.0"


def test_verbose_flag_configures_loguru(monkeypatch):
    """Test --verbose enables loguru with the CLI format."""
    calls: dict[str, object] = {}

    monkeypatch.setattr("atlantis.cli.logger.remove", lambda: calls.__setitem__("removed", True))
    monkeypatch.setattr(
        "atlantis.cli.logger.disable",
        lambda name: calls.setdefault("disabled", []).append(name),
    )
    monkeypatch.setattr(
        "atlantis.cli.logger.enable",
        lambda name: calls.setdefault("enabled", []).append(name),
    )

    def fake_add(sink, **kwargs):
        calls["sink"] = sink
        calls["add_kwargs"] = kwargs
        return 1

    monkeypatch.setattr("atlantis.cli.logger.add", fake_add)

    result = runner.invoke(cli, ["--verbose", "fetch", "--event", "Test_2024"])

    assert result.exit_code == 0
    assert calls["removed"] is True
    assert calls["disabled"] == ["atlantis"]
    assert calls["enabled"] == ["atlantis"]
    assert calls["add_kwargs"]["level"] == "DEBUG"
    assert "DEBUG" not in calls["add_kwargs"]["format"]


def test_no_verbose_keeps_loguru_disabled(monkeypatch):
    """Test that without --verbose, no loguru sink is added."""
    calls: dict[str, object] = {"added": False}

    monkeypatch.setattr("atlantis.cli.logger.remove", lambda: calls.__setitem__("removed", True))
    monkeypatch.setattr(
        "atlantis.cli.logger.disable",
        lambda name: calls.setdefault("disabled", []).append(name),
    )
    monkeypatch.setattr(
        "atlantis.cli.logger.enable",
        lambda name: calls.setdefault("enabled", []).append(name),
    )
    monkeypatch.setattr(
        "atlantis.cli.logger.add",
        lambda *args, **kwargs: calls.__setitem__("added", True),
    )

    result = runner.invoke(cli, ["fetch", "--event", "Test_2024"])

    assert result.exit_code == 0
    assert calls["removed"] is True
    assert calls["disabled"] == ["atlantis"]
    assert "enabled" not in calls
    assert calls["added"] is False


def test_verbose_sink_suppresses_fetcher_logs_during_checklist() -> None:
    """Atlantis logs should not hit stderr while a live checklist is active."""
    fetcher_record = {"name": "atlantis.fetchers.viirs.processor"}
    harmoniser_record = {"name": "atlantis.harmoniser.reprojector"}
    external_record = {"name": "urllib3.connectionpool"}

    assert _should_emit_verbose_log(fetcher_record) is True
    assert _should_emit_verbose_log(harmoniser_record) is True
    assert _should_emit_verbose_log(external_record) is True

    with task_checklist(["Fetch tiles"]):
        assert _should_emit_verbose_log(fetcher_record) is False
        assert _should_emit_verbose_log(harmoniser_record) is False
        assert _should_emit_verbose_log(external_record) is True


def test_fetch_command():
    """Test fetch command with required event argument."""
    result = runner.invoke(cli, ["fetch", "--event", "Valencia_2024"])
    assert result.exit_code == 0
    assert "Valencia_2024" in result.stdout
    assert "sources=" in result.stdout


def test_fetch_command_with_bbox(monkeypatch, tmp_path):
    """Test bbox/date-driven fetch flow for the VIIRS CLI path."""

    class DummyFetcher:
        def __init__(self, **kwargs):
            assert kwargs == {
                "backend": "noaa_s3",
                "data_format": "tif",
                "classify": True,
                "stream": True,
                "strategy": "peak",
                "keep_processed": True,
                "peak_days_before": 0,
                "peak_days_after": 0,
                "max_observations": 0,
                "peak_priority": "post",
            }

        def fetch(self, event, output_dir):
            assert event.event_id == "Yangtze_2020"
            assert event.bbox == (105.0, 28.0, 125.0, 38.0)
            assert output_dir == tmp_path / "viirs"
            return [
                FetchResult(
                    event_id=event.event_id,
                    source_id="viirs",
                    files=[tmp_path / "viirs" / "obs.tif", tmp_path / "viirs" / "mask.tif"],
                    metadata=TileMetadata(
                        event_id=event.event_id,
                        source_id="viirs",
                        fetch_timestamp=datetime.now(timezone.utc),
                        bbox=event.bbox,
                    ),
                )
            ]

    monkeypatch.setattr("atlantis.cli.get_fetcher", lambda _source: DummyFetcher)

    result = runner.invoke(
        cli,
        [
            "fetch",
            "--event",
            "Yangtze_2020",
            "--source",
            "viirs",
            "--output",
            str(tmp_path),
            "--bbox",
            "105 28 125 38",
            "--start-date",
            "2020-07-22",
            "--end-date",
            "2020-07-22",
        ],
    )

    assert result.exit_code == 0
    assert "Wrote 2 files" in result.stdout
    assert "VIIRS backend: noaa_s3" in result.stdout


def test_archive_command(tmp_path):
    """Archive harmonised GeoTIFFs into the consolidated datacube."""
    import numpy as np
    import rioxarray  # noqa: F401
    import xarray as xr

    from atlantis.archive import grid

    row0, col0, h, w = 4000, 10000, 40, 50
    y = grid.global_y_coords()[row0 : row0 + h]
    x = grid.global_x_coords()[col0 : col0 + w]
    da = xr.DataArray(np.full((h, w), 50, dtype="uint8"), dims=["y", "x"], coords={"y": y, "x": x})
    da.rio.write_crs("EPSG:4326", inplace=True)
    harm_dir = tmp_path / "data" / "Valencia_2024" / "viirs" / "harmonised"
    harm_dir.mkdir(parents=True)
    da.rio.to_raster(harm_dir / "Valencia_2024_2020-01-01_viirs_harmonised.tif")

    archive_root = tmp_path / "archive"
    result = runner.invoke(
        cli,
        [
            "archive",
            "--event",
            "Valencia_2024",
            "--input",
            str(tmp_path / "data" / "Valencia_2024"),
            "--archive",
            str(archive_root),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert (archive_root / "datacube.zarr").exists()


def test_fetch_kurosiwo_viirs_command(monkeypatch, tmp_path):
    """Test metadata-driven KuroSiwo VIIRS CLI flow."""

    class DummyFetcher:
        def __init__(self, **kwargs):
            assert kwargs == {
                "backend": "noaa_s3",
                "data_format": "tif",
                "classify": True,
                "stream": True,
                "keep_processed": True,
            }

        def fetch(self, event, output_dir):
            assert isinstance(event, FloodEvent)
            assert event.event_id == "KuroSiwo_470"
            assert event.start_date.isoformat() == "2020-10-14"
            assert event.end_date.isoformat() == "2020-10-14"
            assert output_dir == tmp_path / "KuroSiwo_470" / "viirs"
            return [
                FetchResult(
                    event_id=event.event_id,
                    source_id="viirs",
                    files=[tmp_path / "KuroSiwo_470" / "viirs" / "obs.tif"],
                    metadata=TileMetadata(
                        event_id=event.event_id,
                        source_id="viirs",
                        fetch_timestamp=datetime.now(timezone.utc),
                        bbox=event.bbox,
                    ),
                )
            ]

    monkeypatch.setattr("atlantis.cli.get_fetcher", lambda _source: DummyFetcher)
    monkeypatch.setattr(
        "atlantis.cli.build_kurosiwo_flood_events",
        lambda *args, **kwargs: [
            FloodEvent(
                event_id="KuroSiwo_470",
                bbox=(-0.8627, 8.2639, 1.9947, 11.7312),
                start_date=date(2020, 10, 14),
                end_date=date(2020, 10, 14),
                sources=["viirs"],
            )
        ],
    )

    result = runner.invoke(
        cli,
        [
            "fetch-kurosiwo-viirs",
            "--metadata",
            str(tmp_path / "kurosiwo.csv"),
            "--case",
            "KuroSiwo_470",
            "--output",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Cases selected: 1" in result.stdout
    assert "Fetching KuroSiwo_470" in result.stdout
    assert "Total files written: 1" in result.stdout


def test_fetch_kurosiwo_viirs_command_from_catalogue(monkeypatch, tmp_path):
    """Test catalogue-driven KuroSiwo VIIRS CLI flow."""

    class DummyFetcher:
        def __init__(self, **kwargs):
            assert kwargs == {
                "backend": "noaa_s3",
                "data_format": "tif",
                "classify": True,
                "stream": True,
                "keep_processed": True,
            }

        def fetch(self, event, output_dir):
            assert isinstance(event, FloodEvent)
            assert event.event_id == "KuroSiwo_470"
            assert output_dir == tmp_path / "KuroSiwo_470" / "viirs"
            return [
                FetchResult(
                    event_id=event.event_id,
                    source_id="viirs",
                    files=[tmp_path / "KuroSiwo_470" / "viirs" / "obs.tif"],
                    metadata=TileMetadata(
                        event_id=event.event_id,
                        source_id="viirs",
                        fetch_timestamp=datetime.now(timezone.utc),
                        bbox=event.bbox,
                    ),
                )
            ]

    monkeypatch.setattr("atlantis.cli.get_fetcher", lambda _source: DummyFetcher)
    monkeypatch.setattr(
        "atlantis.cli.build_kurosiwo_flood_events_from_catalogue",
        lambda *args, **kwargs: [
            FloodEvent(
                event_id="KuroSiwo_470",
                bbox=(-0.8627, 8.2639, 1.9947, 11.7312),
                start_date=date(2020, 10, 14),
                end_date=date(2020, 10, 14),
                sources=["viirs"],
            )
        ],
    )

    result = runner.invoke(
        cli,
        [
            "fetch-kurosiwo-viirs",
            "--catalogue",
            str(tmp_path / "catalogue.gpkg"),
            "--case",
            "KuroSiwo_470",
            "--output",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "derived from" in result.stdout
    assert "Total files written: 1" in result.stdout


def test_fetch_kurosiwo_modis_command(monkeypatch, tmp_path):
    """Test metadata-driven KuroSiwo MODIS CLI flow."""

    class DummyFetcher:
        def __init__(self, **kwargs):
            assert kwargs == {
                "backend": "lance_geotiff",
                "composite": "F2",
                "classify": True,
                "stream": True,
                "keep_processed": True,
            }

        def fetch(self, event, output_dir):
            assert isinstance(event, FloodEvent)
            assert event.event_id == "KuroSiwo_470"
            assert output_dir == tmp_path / "KuroSiwo_470" / "modis"
            return [
                FetchResult(
                    event_id=event.event_id,
                    source_id="modis",
                    files=[tmp_path / "KuroSiwo_470" / "modis" / "obs.tif"],
                    metadata=TileMetadata(
                        event_id=event.event_id,
                        source_id="modis",
                        fetch_timestamp=datetime.now(timezone.utc),
                        bbox=event.bbox,
                    ),
                )
            ]

    monkeypatch.setattr("atlantis.cli.get_fetcher", lambda _source: DummyFetcher)
    monkeypatch.setattr(
        "atlantis.cli.build_kurosiwo_flood_events",
        lambda *args, **kwargs: [
            FloodEvent(
                event_id="KuroSiwo_470",
                bbox=(-0.8627, 8.2639, 1.9947, 11.7312),
                start_date=date(2020, 10, 14),
                end_date=date(2020, 10, 14),
                sources=["modis"],
            )
        ],
    )

    result = runner.invoke(
        cli,
        [
            "fetch-kurosiwo-modis",
            "--metadata",
            str(tmp_path / "kurosiwo.csv"),
            "--case",
            "KuroSiwo_470",
            "--output",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Cases selected: 1" in result.stdout
    assert "Fetching KuroSiwo_470" in result.stdout
    assert "Total files written: 1" in result.stdout


def test_fetch_command_supports_legacy_viirs_backend(monkeypatch, tmp_path):
    """Test explicit legacy backend selection for generic VIIRS fetch."""

    class DummyFetcher:
        def __init__(self, **kwargs):
            assert kwargs == {
                "backend": "gmu_legacy",
                "data_format": "tif",
                "classify": True,
                "stream": True,
                "strategy": "peak",
                "keep_processed": True,
                "peak_days_before": 0,
                "peak_days_after": 0,
                "max_observations": 0,
                "peak_priority": "post",
            }

        def fetch(self, event, output_dir):
            return [
                FetchResult(
                    event_id=event.event_id,
                    source_id="viirs",
                    files=[tmp_path / "viirs" / "obs.tif"],
                    metadata=TileMetadata(
                        event_id=event.event_id,
                        source_id="viirs",
                        fetch_timestamp=datetime.now(timezone.utc),
                        bbox=event.bbox,
                    ),
                )
            ]

    monkeypatch.setattr("atlantis.cli.get_fetcher", lambda _source: DummyFetcher)

    result = runner.invoke(
        cli,
        [
            "fetch",
            "--event",
            "Yangtze_2020",
            "--source",
            "viirs",
            "--output",
            str(tmp_path),
            "--bbox",
            "105 28 125 38",
            "--start-date",
            "2020-07-22",
            "--end-date",
            "2020-07-22",
            "--viirs-backend",
            "gmu_legacy",
        ],
    )

    assert result.exit_code == 0
    assert "VIIRS backend: gmu_legacy" in result.stdout
    assert "Legacy backend note" in result.stdout


def test_fetch_command_handles_legacy_backend_offline(monkeypatch, tmp_path):
    """Network errors from the legacy backend must surface as a clean warning, not a traceback."""
    import requests

    class OfflineFetcher:
        def __init__(self, **kwargs):
            self.last_diagnostics = None

        def fetch(self, event, output_dir):
            raise requests.ConnectTimeout("Connection to jpssflood.gmu.edu timed out.")

    monkeypatch.setattr("atlantis.cli.get_fetcher", lambda _source: OfflineFetcher)

    result = runner.invoke(
        cli,
        [
            "fetch",
            "--event",
            "Pakistan_2022",
            "--source",
            "viirs",
            "--output",
            str(tmp_path),
            "--bbox",
            "67.5 26 70 29.5",
            "--start-date",
            "2022-08-28",
            "--end-date",
            "2022-08-28",
            "--viirs-backend",
            "gmu_legacy",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "Network error" in result.stdout
    assert "jpssflood.gmu.edu" in result.stdout
    assert "Traceback" not in result.stdout


def test_build_kurosiwo_metadata_command(monkeypatch, tmp_path):
    """Test CLI command for deriving KuroSiwo metadata from the catalogue."""
    monkeypatch.setattr(
        "atlantis.cli.write_kurosiwo_metadata_csv",
        lambda catalogue_path, output_path: output_path,
    )

    result = runner.invoke(
        cli,
        [
            "build-kurosiwo-metadata",
            "--catalogue",
            str(tmp_path / "catalogue.gpkg"),
            "--output",
            str(tmp_path / "kurosiwo.csv"),
        ],
    )

    assert result.exit_code == 0
    assert "Metadata CSV written" in result.stdout


def test_validate_command():
    """Test validate command."""
    result = runner.invoke(cli, ["validate"])
    assert result.exit_code == 0
    assert "Validating archive" in result.stdout


def test_list_sources_command():
    """Test list-sources command."""
    # Import fetchers to register them first
    from atlantis.fetchers import gfm, rfm, viirs  # noqa: F401

    result = runner.invoke(cli, ["list-sources"])
    assert result.exit_code == 0
    assert "Available Data Sources" in result.stdout
    assert "gfm" in result.stdout
    assert "viirs" in result.stdout
    assert "rfm" in result.stdout


def test_harmonise_command(tmp_path):
    """Test harmonise command with required arguments."""
    import numpy as np
    import rioxarray as rxr  # noqa: F401
    import xarray as xr
    from rasterio.transform import from_bounds

    input_dir = tmp_path / "inputs"
    input_dir.mkdir(parents=True)
    output_dir = tmp_path / "harmonised"

    # Small synthetic flood GeoTIFF
    ds = xr.Dataset(
        {"flood_fraction": xr.DataArray(np.zeros((10, 10), dtype=np.float32), dims=["y", "x"])},
        coords={
            "x": np.linspace(-0.5, 0.5, 10),
            "y": np.linspace(40.5, 39.5, 10),
        },
    )
    ds.rio.write_crs("EPSG:4326", inplace=True)
    ds.rio.write_transform(from_bounds(-0.5, 39.5, 0.5, 40.5, 10, 10), inplace=True)
    tif_path = input_dir / "Valencia_2024_20241029_viirs_flood_fraction.tif"
    ds["flood_fraction"].rio.to_raster(str(tif_path), dtype="float32", compress="LZW")

    result = runner.invoke(
        cli,
        [
            "harmonise",
            "--event",
            "Valencia_2024",
            "--source",
            "viirs",
            "--input",
            str(input_dir),
            "--output",
            str(output_dir),
        ],
    )
    assert result.exit_code == 0, f"CLI failed: {result.stdout}"
    assert "Harmonising" in result.stdout
    assert "1 file(s)" in result.stdout


def test_demo_command(monkeypatch, tmp_path):
    """Test demo command runs the checklist-wrapped happy path."""

    fetch_result = FetchResult(
        event_id="Valencia_2024",
        source_id="viirs",
        files=[tmp_path / "viirs" / "obs.tif"],
        metadata=TileMetadata(
            event_id="Valencia_2024",
            source_id="viirs",
            fetch_timestamp=datetime.now(timezone.utc),
            bbox=(-1.5, 38.8, 0.5, 40.0),
        ),
    )

    class DummyFetcher:
        def __init__(self, **kwargs):
            assert kwargs == {
                "classify": True,
                "stream": True,
                "strategy": "peak",
                "keep_processed": True,
            }

        def fetch(self, event, output_dir):
            assert event.event_id == "Valencia_2024"
            assert output_dir == tmp_path / "viirs"
            return [fetch_result]

        def to_dataset(self, result):
            assert result is fetch_result
            return _FakeDataset({"flood_fraction": np.ones((4, 4), dtype=np.float32)})

    monkeypatch.setattr("atlantis.utils.setup.get_missing_assets", lambda: [])
    monkeypatch.setattr("atlantis.cli.get_fetcher", lambda _source: DummyFetcher)
    monkeypatch.setattr("atlantis.cli._plot_viirs", lambda *args, **kwargs: None)
    monkeypatch.setattr("atlantis.cli._harmonise_viirs", lambda *args, **kwargs: None)

    result = runner.invoke(
        cli,
        [
            "demo",
            "--output",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Valencia_2024" in result.stdout
    assert "Demo complete!" in result.stdout
    assert "Saved:" not in result.stdout
    assert "Wrote 1 files" not in result.stdout


# ── Unit tests for helper functions ──────────────────────────────────────────


class TestSelectBestResult:
    """Tests for ``_select_best_result``."""

    def test_picks_highest_flood_count(self, tmp_path):
        r1, ds1 = _dummy_fetch_result("Ev", tmp_path, date_token="20200722", flood_pixels=2)
        r2, ds2 = _dummy_fetch_result("Ev", tmp_path, date_token="20200723", flood_pixels=8)
        r3, ds3 = _dummy_fetch_result("Ev", tmp_path, date_token="20200724", flood_pixels=1)
        ds_map = {r1.files[0]: ds1, r2.files[0]: ds2, r3.files[0]: ds3}

        class _Fetcher:
            def to_dataset(self, result):
                return ds_map[result.files[0]]

        best, label = _select_best_result(_Fetcher(), [r1, r2, r3])
        assert best is r2
        assert label == "2020-07-23"

    def test_falls_back_to_first_when_no_flood(self, tmp_path):
        r1, ds1 = _dummy_fetch_result("Ev", tmp_path, date_token="20200722", flood_pixels=0)

        class _Fetcher:
            def to_dataset(self, _):
                return ds1

        best, label = _select_best_result(_Fetcher(), [r1])
        assert best is r1
        assert "2020-07-22" in label


class TestPlotViirs:
    """Tests for ``_plot_viirs``."""

    def test_calls_plot_classified_for_flood_extent(self, tmp_path, monkeypatch):
        calls: list[dict] = []

        def _capture(da, *, title, output_path, announce=True):
            calls.append({"da": da, "title": title, "path": output_path})

        monkeypatch.setattr("atlantis.cli.plot_classified", _capture)
        ds = _FakeDataset({"flood_fraction": np.ones((4, 4), dtype=np.float32)})
        out = tmp_path / "plot.png"
        _plot_viirs(ds, "Ev", "2020-07-22", output_png_path=out)
        assert len(calls) == 1
        assert "flood extent" in calls[0]["title"]
        assert "375 m" in calls[0]["title"]
        assert calls[0]["path"] == out

    def test_calls_plot_raw_when_no_flood(self, tmp_path, monkeypatch):
        calls: list[dict] = []

        def _capture(da, *, title, output_path, announce=True):
            calls.append({"da": da, "title": title, "path": output_path})

        monkeypatch.setattr("atlantis.cli.plot_raw", _capture)
        ds = _FakeDataset({"raw": np.ones((4, 4), dtype=np.float32)})
        out = tmp_path / "plot.png"
        _plot_viirs(ds, "Ev", "2020-07-22", output_png_path=out)
        assert len(calls) == 1
        assert "raw composite" in calls[0]["title"]


# ── CLI integration tests for --plot / --harmonise ────────


def _make_fetcher_ds_map(tmp_path, event_id="Yangtze_2020", date_token="20200722"):
    """Return (DummyFetcher, ds_dict) for use in fetch tests."""
    fetch_result, ds = _dummy_fetch_result(event_id, tmp_path, date_token=date_token, flood_pixels=5)
    in_memory_result = FetchResult(
        event_id=event_id,
        source_id="viirs",
        files=[],
        metadata=fetch_result.metadata,
        date_token=date_token,
        dataset=ds,  # type: ignore[arg-type]
    )

    class DummyFetcher:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def fetch(self, event, output_dir):
            if self.kwargs.get("keep_processed", True):
                return [fetch_result]
            return [in_memory_result]

        def to_dataset(self, result):
            if result.dataset is not None:
                return result.dataset
            return ds

    return DummyFetcher, fetch_result, ds


def test_fetch_with_plot_saves_png(monkeypatch, tmp_path):
    """``--plot`` should call ``plot_classified`` for the 375 m output."""
    DummyFetcher, fetch_result, ds = _make_fetcher_ds_map(tmp_path)
    monkeypatch.setattr("atlantis.cli.get_fetcher", lambda _s: DummyFetcher)

    plot_calls: list = []
    monkeypatch.setattr(
        "atlantis.cli.plot_classified",
        lambda da, *, title, output_path, announce=True: plot_calls.append(output_path),
    )

    result = runner.invoke(
        cli,
        [
            "fetch",
            "--event",
            "Yangtze_2020",
            "--source",
            "viirs",
            "--output",
            str(tmp_path),
            "--bbox",
            "105 28 125 38",
            "--start-date",
            "2020-07-22",
            "--end-date",
            "2020-07-22",
            "--plot",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert any("viirs.png" in str(p) for p in plot_calls)


def test_fetch_with_harmonise_saves_tif_and_png(monkeypatch, tmp_path):
    """``--harmonise`` should save harmonised TIF + PNG and not remove processed/."""
    DummyFetcher, fetch_result, ds = _make_fetcher_ds_map(tmp_path)
    monkeypatch.setattr("atlantis.cli.get_fetcher", lambda _s: DummyFetcher)

    # Create processed/ dir so we can verify it survives
    processed_dir = tmp_path / "viirs" / "processed"
    processed_dir.mkdir(parents=True)
    (processed_dir / "dummy.tif").touch()

    # Mock Harmoniser to return a dataset with flood_extent + rio.to_raster
    harm_flood = np.ones((4, 4), dtype=np.float32) * 0.5
    harm_da = MagicMock()
    harm_da.values = harm_flood
    harm_da.max.return_value = 0.5

    harm_ds = MagicMock()
    harm_ds.__contains__ = lambda self, k: k == "flood_fraction"
    harm_ds.__getitem__ = lambda self, k: harm_da
    harm_ds.__iter__ = lambda self: iter(["flood_fraction"])
    harm_ds.data_vars = ["flood_fraction"]

    mock_harmoniser_cls = MagicMock()
    mock_harmoniser_cls.return_value.harmonise.return_value = harm_ds
    monkeypatch.setattr("atlantis.harmoniser.Harmoniser", mock_harmoniser_cls)

    write_calls: list = []
    monkeypatch.setattr(
        "atlantis.cli.write_harmonised_raster",
        lambda da, path: write_calls.append(path),
    )

    plot_calls: list = []
    monkeypatch.setattr(
        "atlantis.cli.plot_classified",
        lambda da, *, title, output_path, announce=True: plot_calls.append(output_path),
    )

    result = runner.invoke(
        cli,
        [
            "fetch",
            "--event",
            "Yangtze_2020",
            "--source",
            "viirs",
            "--output",
            str(tmp_path),
            "--bbox",
            "105 28 125 38",
            "--start-date",
            "2020-07-22",
            "--end-date",
            "2020-07-22",
            "--harmonise",
        ],
    )
    assert result.exit_code == 0, result.stdout
    # Harmonised PNG should have been saved
    assert any("harmonised" in str(p) and p.suffix == ".png" for p in plot_calls)
    # TIF was written via write_harmonised_raster
    assert len(write_calls) == 1
    # processed/ should still exist
    assert processed_dir.exists()
    assert (processed_dir / "dummy.tif").exists()


def test_fetch_no_keep_processed_skips_processed_on_disk(monkeypatch, tmp_path):
    """``--no-keep-processed`` should fetch in memory and not create processed/."""
    DummyFetcher, fetch_result, ds = _make_fetcher_ds_map(tmp_path)
    captured_kwargs: list[dict] = []

    class RecordingFetcher(DummyFetcher):
        def __init__(self, **kwargs):
            captured_kwargs.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr("atlantis.cli.get_fetcher", lambda _s: RecordingFetcher)

    processed_dir = tmp_path / "viirs" / "processed"

    # Mock Harmoniser
    harm_flood = np.ones((4, 4), dtype=np.float32)
    harm_da = MagicMock()
    harm_da.values = harm_flood

    harm_ds = MagicMock()
    harm_ds.__contains__ = lambda self, k: k == "flood_fraction"
    harm_ds.__getitem__ = lambda self, k: harm_da
    harm_ds.__iter__ = lambda self: iter(["flood_fraction"])
    harm_ds.data_vars = ["flood_fraction"]

    mock_harmoniser_cls = MagicMock()
    mock_harmoniser_cls.return_value.harmonise.return_value = harm_ds
    monkeypatch.setattr("atlantis.harmoniser.Harmoniser", mock_harmoniser_cls)

    monkeypatch.setattr("atlantis.cli.plot_classified", lambda *a, **kw: None)

    result = runner.invoke(
        cli,
        [
            "fetch",
            "--event",
            "Yangtze_2020",
            "--source",
            "viirs",
            "--output",
            str(tmp_path),
            "--bbox",
            "105 28 125 38",
            "--start-date",
            "2020-07-22",
            "--end-date",
            "2020-07-22",
            "--no-keep-processed",
            "--harmonise",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert captured_kwargs and captured_kwargs[0].get("keep_processed") is False
    assert "Peak-flood date" in result.stdout
    assert not processed_dir.exists()


def test_fetch_plot_without_harmonise_no_harmonised_dir(monkeypatch, tmp_path):
    """``--plot`` alone should not create harmonised/ directory."""
    DummyFetcher, fetch_result, ds = _make_fetcher_ds_map(tmp_path)
    monkeypatch.setattr("atlantis.cli.get_fetcher", lambda _s: DummyFetcher)
    monkeypatch.setattr("atlantis.cli.plot_classified", lambda *a, **kw: None)

    result = runner.invoke(
        cli,
        [
            "fetch",
            "--event",
            "Yangtze_2020",
            "--source",
            "viirs",
            "--output",
            str(tmp_path),
            "--bbox",
            "105 28 125 38",
            "--start-date",
            "2020-07-22",
            "--end-date",
            "2020-07-22",
            "--plot",
        ],
    )
    assert result.exit_code == 0, result.stdout
    harm_dir = tmp_path / "viirs" / "harmonised"
    assert not harm_dir.exists()


def test_fetch_kurosiwo_with_harmonise_and_harmonise_only(monkeypatch, tmp_path):
    """KuroSiwo command: harmonise and remove processed/."""
    fetched_date_token = "20201014"
    event_id = "KuroSiwo_470"
    fetch_result, ds = _dummy_fetch_result(event_id, tmp_path, date_token=fetched_date_token, flood_pixels=5)

    in_memory_result = FetchResult(
        event_id=event_id,
        source_id="viirs",
        files=[],
        metadata=fetch_result.metadata,
        date_token=fetched_date_token,
        dataset=ds,  # type: ignore[arg-type]
    )

    class DummyFetcher:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def fetch(self, event, output_dir):
            if self.kwargs.get("keep_processed", True):
                return [fetch_result]
            return [in_memory_result]

        def to_dataset(self, result):
            if result.dataset is not None:
                return result.dataset
            return ds

    monkeypatch.setattr("atlantis.cli.get_fetcher", lambda _s: DummyFetcher)
    monkeypatch.setattr(
        "atlantis.cli.build_kurosiwo_flood_events",
        lambda *a, **kw: [
            FloodEvent(
                event_id=event_id,
                bbox=(-0.8627, 8.2639, 1.9947, 11.7312),
                start_date=date(2020, 10, 14),
                end_date=date(2020, 10, 14),
                sources=["viirs"],
            )
        ],
    )

    # Mock Harmoniser
    harm_flood = np.ones((4, 4), dtype=np.float32)
    harm_da = MagicMock()
    harm_da.values = harm_flood

    harm_ds = MagicMock()
    harm_ds.__contains__ = lambda self, k: k == "flood_fraction"
    harm_ds.__getitem__ = lambda self, k: harm_da
    harm_ds.__iter__ = lambda self: iter(["flood_fraction"])
    harm_ds.data_vars = ["flood_fraction"]

    mock_harmoniser_cls = MagicMock()
    mock_harmoniser_cls.return_value.harmonise.return_value = harm_ds
    monkeypatch.setattr("atlantis.harmoniser.Harmoniser", mock_harmoniser_cls)

    plot_calls: list = []
    monkeypatch.setattr(
        "atlantis.cli.plot_classified",
        lambda da, *, title, output_path, announce=True: plot_calls.append(output_path),
    )

    write_calls: list = []
    monkeypatch.setattr(
        "atlantis.cli.write_harmonised_raster",
        lambda da, path: write_calls.append(path),
    )

    event_viirs_dir = tmp_path / event_id / "viirs"
    processed_dir = event_viirs_dir / "processed"

    result = runner.invoke(
        cli,
        [
            "fetch-kurosiwo-viirs",
            "--metadata",
            str(tmp_path / "kurosiwo.csv"),
            "--case",
            event_id,
            "--output",
            str(tmp_path),
            "--no-keep-processed",
            "--harmonise",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "Fetching KuroSiwo_470" in result.stdout
    # Harmonised PNG saved
    assert any("harmonised" in str(p) and p.suffix == ".png" for p in plot_calls)
    # TIF written via write_harmonised_raster
    assert len(write_calls) == 1
    assert "processed in memory" in result.stdout
    assert not processed_dir.exists()
    # raw/ should still exist (not created here but not deleted)
    assert not (event_viirs_dir / "raw").exists()  # wasn't created by DummyFetcher


# ── Peak-window CLI forwarding tests ──────────────────────────────────────────


def test_fetch_peak_window_days_forwarded(monkeypatch, tmp_path):
    """--peak-window-days sets both peak_days_before and peak_days_after."""
    captured: list[dict] = []

    class RecordFetcher:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def fetch(self, event, output_dir):
            return []

    monkeypatch.setattr("atlantis.cli.get_fetcher", lambda _s: RecordFetcher)

    result = runner.invoke(
        cli,
        [
            "fetch",
            "--event",
            "Yangtze_2020",
            "--source",
            "viirs",
            "--output",
            str(tmp_path),
            "--bbox",
            "105 28 125 38",
            "--start-date",
            "2020-07-22",
            "--end-date",
            "2020-07-22",
            "--strategy",
            "all",
            "--peak-window-days",
            "7",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured
    assert captured[0]["peak_days_before"] == 7
    assert captured[0]["peak_days_after"] == 7


def test_fetch_peak_days_asymmetric_forwarded(monkeypatch, tmp_path):
    """--peak-days-before and --peak-days-after can be set independently."""
    captured: list[dict] = []

    class RecordFetcher:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def fetch(self, event, output_dir):
            return []

    monkeypatch.setattr("atlantis.cli.get_fetcher", lambda _s: RecordFetcher)

    result = runner.invoke(
        cli,
        [
            "fetch",
            "--event",
            "Yangtze_2020",
            "--source",
            "viirs",
            "--output",
            str(tmp_path),
            "--bbox",
            "105 28 125 38",
            "--start-date",
            "2020-07-22",
            "--end-date",
            "2020-07-22",
            "--peak-days-before",
            "3",
            "--peak-days-after",
            "5",
            "--max-observations",
            "4",
            "--peak-priority",
            "balanced",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured
    assert captured[0]["peak_days_before"] == 3
    assert captured[0]["peak_days_after"] == 5
    assert captured[0]["max_observations"] == 4
    assert captured[0]["peak_priority"] == "balanced"


def test_fetch_peak_window_days_mutex_with_specific_flags(monkeypatch, tmp_path):
    """--peak-window-days and --peak-days-before are mutually exclusive."""
    monkeypatch.setattr("atlantis.cli.get_fetcher", lambda _s: object)

    result = runner.invoke(
        cli,
        [
            "fetch",
            "--event",
            "Yangtze_2020",
            "--source",
            "viirs",
            "--output",
            str(tmp_path),
            "--bbox",
            "105 28 125 38",
            "--start-date",
            "2020-07-22",
            "--end-date",
            "2020-07-22",
            "--peak-window-days",
            "7",
            "--peak-days-before",
            "3",
        ],
    )
    assert result.exit_code != 0 or "cannot be combined" in result.output


# ── GFM CLI warnings ─────────────────────────────────────────────────────────


def test_gfm_warns_on_no_stream():
    """GFM should warn when --no-stream is passed (always streams via STAC/COG)."""
    from unittest.mock import patch

    with patch("atlantis.cli.get_fetcher") as mock_get:
        mock_fetcher_cls = MagicMock()
        mock_get.return_value = mock_fetcher_cls
        mock_fetcher_cls.return_value.fetch.return_value = []

        result = runner.invoke(
            cli,
            [
                "fetch",
                "--event",
                "Test_2024",
                "--source",
                "gfm",
                "--bbox",
                "-1 38 0 39",
                "--start-date",
                "2024-01-01",
                "--end-date",
                "2024-01-02",
                "--no-stream",
            ],
        )
        assert "GFM always streams" in result.output


def test_gfm_warns_on_no_classify():
    """GFM should warn when --no-classify is passed."""
    from unittest.mock import patch

    with patch("atlantis.cli.get_fetcher") as mock_get:
        mock_fetcher_cls = MagicMock()
        mock_get.return_value = mock_fetcher_cls
        mock_fetcher_cls.return_value.fetch.return_value = []

        result = runner.invoke(
            cli,
            [
                "fetch",
                "--event",
                "Test_2024",
                "--source",
                "gfm",
                "--bbox",
                "-1 38 0 39",
                "--start-date",
                "2024-01-01",
                "--end-date",
                "2024-01-02",
                "--no-classify",
            ],
        )
        assert "GFM always produces classified layers" in result.output


def test_gfm_harmonise_always_enabled():
    """GFM should emit info that harmonise is enabled by default when not explicitly passed."""
    from unittest.mock import patch

    with patch("atlantis.cli.get_fetcher") as mock_get:
        mock_fetcher_cls = MagicMock()
        mock_get.return_value = mock_fetcher_cls
        mock_fetcher_cls.return_value.fetch.return_value = []

        result = runner.invoke(
            cli,
            [
                "fetch",
                "--event",
                "Test_2024",
                "--source",
                "gfm",
                "--bbox",
                "-1 38 0 39",
                "--start-date",
                "2024-01-01",
                "--end-date",
                "2024-01-02",
            ],
        )
        assert "harmonised output enabled by default" in result.output
