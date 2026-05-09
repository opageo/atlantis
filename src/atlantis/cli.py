"""CLI entrypoints for Atlantis."""

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from atlantis.config import HarmoniseConfig, get_config

# Import fetchers to register them
from atlantis.fetchers import fetcher_registry, get_fetcher, gfm, list_fetchers, rfm, viirs  # noqa: F401

cli = typer.Typer(help="Atlantis — ML-ready flood inundation archive pipeline.")
console = Console()


@cli.command()
def fetch(
    event: str = typer.Option(..., "--event", "-e", help="Flood event ID"),
    source: str | None = typer.Option(None, "--source", "-s", help="Data source (gfm, viirs, rfm, all)"),
    output_dir: Path | None = typer.Option(None, "--output", "-o", help="Output directory for raw data"),
) -> None:
    """Fetch raw inundation data from specified source(s).

    Args:
        event: Flood event ID to fetch data for.
        source: Data source to fetch from. Options: gfm, viirs, rfm, all.
        output_dir: Directory to save downloaded files.
    """
    config = get_config()
    output_dir = output_dir or config.fetcher.cache_dir / "raw" / event
    output_dir.mkdir(parents=True, exist_ok=True)

    if source is None or source == "all":
        sources = list_fetchers()
    else:
        sources = [source]

    console.print(f"[bold]Fetching data for event:[/bold] {event}")
    console.print(f"[bold]Sources:[/bold] {', '.join(sources)}")
    console.print(f"[bold]Output:[/bold] {output_dir}")

    for src in sources:
        try:
            fetcher_cls = get_fetcher(src)
            console.print(f"\n[cyan]Fetching from {src}...[/cyan]")
            # Validate fetcher can be instantiated
            _ = fetcher_cls()  # noqa: F841
            # TODO: Create FloodEvent and call fetcher.search() and fetch()
            console.print(f"[yellow]  {src}: not yet implemented[/yellow]")
        except KeyError:
            console.print(f"[red]Error: Unknown source '{src}'[/red]")


@cli.command()
def harmonise(
    event: str = typer.Option(..., "--event", "-e", help="Flood event ID"),
    source: str = typer.Option(..., "--source", "-s", help="Data source ID"),
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Harmonisation config file (YAML)"),
    output_dir: Path | None = typer.Option(None, "--output", "-o", help="Output directory for harmonised data"),
) -> None:
    """Harmonise fetched data (reproject, tile, normalise).

    Args:
        event: Flood event ID to harmonise.
        source: Data source ID.
        config_path: Optional path to harmonisation config.
        output_dir: Directory to save harmonised data.
    """
    console.print(f"[bold]Harmonising data for event:[/bold] {event}")
    console.print(f"[bold]Source:[/bold] {source}")

    if config_path:
        console.print(f"[bold]Config:[/bold] {config_path}")
        # TODO: Load config from YAML
    else:
        harmonise_config = HarmoniseConfig()
        console.print(
            f"[bold]Using defaults:[/bold] CRS={harmonise_config.target_crs}, tile_size={harmonise_config.tile_size}"
        )

    console.print("[yellow]Harmonisation not yet implemented[/yellow]")


@cli.command()
def archive(
    event: str = typer.Option(..., "--event", "-e", help="Flood event ID"),
    source: str | None = typer.Option(None, "--source", "-s", help="Data source (default: all available)"),
    archive_root: Path | None = typer.Option(None, "--archive", "-a", help="Archive root directory"),
    raw_only: bool = typer.Option(False, "--raw-only", help="Only write raw archive (skip ML-ready)"),
) -> None:
    """Write harmonised data to Zarr archive (raw + ML-ready).

    Args:
        event: Flood event ID to archive.
        source: Data source (default: all available).
        archive_root: Root directory for archive storage.
        raw_only: Skip ML-ready archive (write raw only).
    """
    config = get_config()
    archive_root = archive_root or config.archive.archive_root

    console.print(f"[bold]Archiving event:[/bold] {event}")
    console.print(f"[bold]Archive root:[/bold] {archive_root}")

    if source:
        console.print(f"[bold]Source:[/bold] {source}")
    else:
        console.print("[bold]Source:[/bold] all available")

    if raw_only:
        console.print("[yellow]Writing raw archive only[/yellow]")
    else:
        console.print("[yellow]Writing raw + ML-ready archives...[/yellow]")

    console.print("[yellow]Archive writing not yet implemented[/yellow]")


@cli.command()
def validate(
    event: str | None = typer.Option(None, "--event", "-e", help="Event ID to validate"),
    source: str | None = typer.Option(None, "--source", "-s", help="Source ID to validate"),
    archive_root: Path | None = typer.Option(None, "--archive", "-a", help="Archive root directory"),
    check_ml: bool = typer.Option(False, "--check-ml", help="Also run ML validation (PyTorch smoke test)"),
) -> None:
    """Validate archive integrity and optionally test ML loading.

    Args:
        event: Event ID to validate. If None, validates all events.
        source: Source ID to validate. If None, validates all sources.
        archive_root: Archive root directory.
        check_ml: Also run ML-specific validation tests.
    """
    config = get_config()
    archive_root = archive_root or config.archive.archive_root

    console.print(f"[bold]Validating archive:[/bold] {archive_root}")

    if event:
        console.print(f"[bold]Event:[/bold] {event}")
    else:
        console.print("[bold]Event:[/bold] all")

    if source:
        console.print(f"[bold]Source:[/bold] {source}")
    else:
        console.print("[bold]Source:[/bold] all")

    if check_ml:
        console.print("[cyan]ML validation: enabled[/cyan]")

    console.print("[yellow]Validation not yet implemented[/yellow]")


@cli.command("list-sources")
def list_sources_cmd() -> None:
    """List all available data sources."""
    sources = list_fetchers()

    table = Table(title="Available Data Sources")
    table.add_column("Name", style="cyan")
    table.add_column("Description", style="white")

    source_descriptions = {
        "gfm": "Global Flood Monitor (STAC/EODC)",
        "viirs": "VIIRS Flood Detection (NOAA)",
        "rfm": "Regional Flood Model (Phase C)",
    }

    for src in sources:
        description = source_descriptions.get(src, "No description")
        table.add_row(src, description)

    console.print(table)


@cli.command("list-events")
def list_events_cmd(
    archive_root: Path | None = typer.Option(None, "--archive", "-a", help="Archive root directory"),
) -> None:
    """List all events in the archive.

    Args:
        archive_root: Archive root directory.
    """
    config = get_config()
    archive_root = archive_root or config.archive.archive_root

    console.print(f"[bold]Archive:[/bold] {archive_root}")

    # TODO: Implement event listing
    console.print("[yellow]No events found (archive not yet implemented)[/yellow]")


if __name__ == "__main__":
    cli()
