"""Tests for layer discovery: programmatic API, docs rendering, and CLI."""

from __future__ import annotations

from typer.testing import CliRunner

from atlantis.cli import cli
from atlantis.layers import available_sources, list_layers
from atlantis.layers.docs import render_all_markdown

runner = CliRunner()


def test_available_sources_includes_all_three() -> None:
    sources = available_sources()
    assert {"modis", "viirs", "gfm"}.issubset(set(sources))


def test_list_layers_returns_native_then_derived() -> None:
    layers = list_layers("modis")
    names = [layer.name for layer in layers]
    # raw native first, derived flood_fraction present.
    assert names[0] == "raw"
    assert "flood_fraction" in names
    assert "recurring_flood" in names


def test_render_all_markdown_documents_each_source() -> None:
    md = render_all_markdown()
    assert "## modis" in md
    assert "## viirs" in md
    assert "## gfm" in md
    # flood_fraction is documented as a derived layer.
    assert "flood_fraction" in md
    assert "Derived layers" in md


def test_cli_list_layers_all_sources() -> None:
    result = runner.invoke(cli, ["list-layers"])
    assert result.exit_code == 0
    assert "modis layers" in result.stdout
    assert "gfm layers" in result.stdout


def test_cli_list_layers_single_source() -> None:
    result = runner.invoke(cli, ["list-layers", "--source", "viirs"])
    assert result.exit_code == 0
    assert "viirs layers" in result.stdout
    assert "flood_fraction" in result.stdout


def test_cli_list_layers_unknown_source_errors() -> None:
    result = runner.invoke(cli, ["list-layers", "--source", "nope"])
    assert result.exit_code == 1
