"""Tests for CLI commands."""

from typer.testing import CliRunner

from atlantis import __version__
from atlantis.cli import cli

runner = CliRunner()


def test_version():
    """Test version is correctly set."""
    assert __version__ == "0.1.0"


def test_fetch_command():
    """Test fetch command with required event argument."""
    result = runner.invoke(cli, ["fetch", "--event", "Valencia_2024"])
    assert result.exit_code == 0
    assert "Fetching data for event: Valencia_2024" in result.stdout


def test_archive_command():
    """Test archive command with required event argument."""
    result = runner.invoke(cli, ["archive", "--event", "Valencia_2024"])
    assert result.exit_code == 0
    assert "Archiving event: Valencia_2024" in result.stdout


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
