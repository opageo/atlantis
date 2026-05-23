"""Tests for CLI commands."""

from datetime import date, datetime, timezone

from typer.testing import CliRunner

from atlantis import __version__
from atlantis.cli import cli
from atlantis.fetchers.base import FetchResult
from atlantis.models.event import FloodEvent
from atlantis.models.metadata import TileMetadata

runner = CliRunner()


def test_version():
    """Test version is correctly set."""
    assert __version__ == "0.1.0"


def test_fetch_command():
    """Test fetch command with required event argument."""
    result = runner.invoke(cli, ["fetch", "--event", "Valencia_2024"])
    assert result.exit_code == 0
    assert "Fetching data for event: Valencia_2024" in result.stdout


def test_fetch_command_with_bbox(monkeypatch, tmp_path):
    """Test bbox/date-driven fetch flow for the VIIRS CLI path."""

    class DummyFetcher:
        def __init__(self, **kwargs):
            assert kwargs == {"backend": "noaa_s3", "data_format": "tif"}

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


def test_archive_command():
    """Test archive command with required event argument."""
    result = runner.invoke(cli, ["archive", "--event", "Valencia_2024"])
    assert result.exit_code == 0
    assert "Archiving event: Valencia_2024" in result.stdout


def test_fetch_kurosiwo_viirs_command(monkeypatch, tmp_path):
    """Test metadata-driven KuroSiwo VIIRS CLI flow."""

    class DummyFetcher:
        def __init__(self, **kwargs):
            assert kwargs == {"backend": "noaa_s3", "data_format": "tif"}

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
            assert kwargs == {"backend": "noaa_s3", "data_format": "tif"}

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


def test_fetch_command_supports_legacy_viirs_backend(monkeypatch, tmp_path):
    """Test explicit legacy backend selection for generic VIIRS fetch."""

    class DummyFetcher:
        def __init__(self, **kwargs):
            assert kwargs == {"backend": "gmu_legacy", "data_format": "tif"}

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


def test_harmonise_command():
    """Test harmonise command with required arguments."""
    result = runner.invoke(cli, ["harmonise", "--event", "Valencia_2024", "--source", "gfm"])
    assert result.exit_code == 0
    assert "Harmonising data for event: Valencia_2024" in result.stdout
    assert "Source: gfm" in result.stdout
