from typer.testing import CliRunner

from atlantis import __version__
from atlantis.cli import cli

runner = CliRunner()


def test_version():
    assert __version__ == "0.1.0"


def test_fetch_command():
    result = runner.invoke(cli, ["fetch"])
    assert result.exit_code == 0
    assert "fetch: not yet implemented" in result.stdout


def test_archive_command():
    result = runner.invoke(cli, ["archive"])
    assert result.exit_code == 0
    assert "archive: not yet implemented" in result.stdout


def test_validate_command():
    result = runner.invoke(cli, ["validate"])
    assert result.exit_code == 0
    assert "validate: not yet implemented" in result.stdout
