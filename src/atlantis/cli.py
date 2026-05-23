"""CLI entrypoints for Atlantis."""

from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from atlantis.config import HarmoniseConfig, get_config

# Import fetchers to register them
from atlantis.fetchers import fetcher_registry, get_fetcher, gfm, list_fetchers, rfm, viirs  # noqa: F401
from atlantis.models.event import FloodEvent
from atlantis.utils.kurosiwo import build_kurosiwo_flood_events

cli = typer.Typer(help="Atlantis — ML-ready flood inundation archive pipeline.")
console = Console()


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    """Parse a bbox from a four-number string."""
    parts = value.replace(",", " ").split()
    if len(parts) != 4:
        raise typer.BadParameter("BBox must contain exactly four numbers: west south east north")
    west, south, east, north = (float(part) for part in parts)
    return (west, south, east, north)


def _parse_date(value: str, option_name: str) -> date:
    """Parse a YYYY-MM-DD date option."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{option_name} must be in YYYY-MM-DD format") from exc


@cli.command()
def fetch(
    event: str = typer.Option(..., "--event", "-e", help="Flood event ID"),
    source: str | None = typer.Option(None, "--source", "-s", help="Data source (gfm, viirs, rfm, all)"),
    output_dir: Path | None = typer.Option(None, "--output", "-o", help="Output directory for raw data"),
    bbox: str | None = typer.Option(None, "--bbox", help="Bounding box as 'west south east north'"),
    start_date: str | None = typer.Option(None, "--start-date", help="Start date in YYYY-MM-DD format"),
    end_date: str | None = typer.Option(None, "--end-date", help="End date in YYYY-MM-DD format"),
) -> None:
    """Fetch raw inundation data from specified source(s).

    Args:
        event: Flood event ID to fetch data for.
        source: Data source to fetch from. Options: gfm, viirs, rfm, all.
        output_dir: Directory to save downloaded files.
        bbox: Bounding box as west south east north for direct event construction.
        start_date: Start date for direct event construction in YYYY-MM-DD format.
        end_date: End date for direct event construction in YYYY-MM-DD format.
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

    flood_event: FloodEvent | None = None
    if bbox or start_date or end_date:
        if not (bbox and start_date and end_date):
            raise typer.BadParameter("--bbox, --start-date and --end-date must be provided together")
        flood_event = FloodEvent(
            event_id=event,
            bbox=_parse_bbox(bbox),
            start_date=_parse_date(start_date, "start-date"),
            end_date=_parse_date(end_date, "end-date"),
            sources=sources,
        )

    for src in sources:
        try:
            fetcher_cls = get_fetcher(src)
            console.print(f"\n[cyan]Fetching from {src}...[/cyan]")
            fetcher = fetcher_cls()
            if flood_event is None:
                console.print(
                    "[yellow]  Event catalogue lookup not yet implemented; "
                    "provide --bbox/--start-date/--end-date[/yellow]"
                )
                continue

            fetch_results = fetcher.fetch(flood_event, output_dir / src)
            if not fetch_results:
                console.print("[yellow]  No files were fetched[/yellow]")
                continue

            console.print(f"[bold]  Wrote {sum(len(result.files) for result in fetch_results)} files[/bold]")
            for result in fetch_results:
                for path in result.files:
                    console.print(f"  - {path}")
        except KeyError:
            console.print(f"[red]Error: Unknown source '{src}'[/red]")


@cli.command("fetch-kurosiwo-viirs")
def fetch_kurosiwo_viirs(
    metadata_path: Path = typer.Option(
        Path("notebooks/drafts/kurosiwo_metadata_v1.csv"),
        "--metadata",
        help="Path to KuroSiwo metadata CSV",
    ),
    case: str | None = typer.Option(None, "--case", help="Only fetch one KuroSiwo flood_case"),
    limit: int | None = typer.Option(None, "--limit", help="Only process the first N cases after filtering"),
    output_dir: Path | None = typer.Option(None, "--output", "-o", help="Output directory for VIIRS products"),
    days_before: int = typer.Option(
        0,
        "--days-before",
        help="Days before KuroSiwo date_end to include in the VIIRS search window",
    ),
    days_after: int = typer.Option(
        0,
        "--days-after",
        help="Days after KuroSiwo date_end to include in the VIIRS search window",
    ),
    use_metadata_range: bool = typer.Option(
        False,
        "--use-metadata-range",
        help="Use date_start..date_end from the metadata CSV instead of a narrow window around date_end",
    ),
) -> None:
    """Fetch VIIRS data for KuroSiwo cases using the derived metadata CSV."""
    config = get_config()
    output_root = output_dir or config.fetcher.cache_dir / "raw" / "kurosiwo"
    output_root.mkdir(parents=True, exist_ok=True)

    events = build_kurosiwo_flood_events(
        metadata_path,
        case=case,
        limit=limit,
        days_before=days_before,
        days_after=days_after,
        use_metadata_range=use_metadata_range,
    )
    fetcher_cls = get_fetcher("viirs")
    fetcher = fetcher_cls()

    console.print(f"[bold]KuroSiwo metadata:[/bold] {metadata_path}")
    console.print(f"[bold]Cases selected:[/bold] {len(events)}")
    console.print(f"[bold]Output root:[/bold] {output_root}")

    total_files = 0
    failures: list[tuple[str, str]] = []

    for event in events:
        console.print(
            f"\n[cyan]Fetching {event.event_id}[/cyan] ({event.start_date.isoformat()} -> {event.end_date.isoformat()})"
        )
        try:
            fetch_results = fetcher.fetch(event, output_root / event.event_id / "viirs")
        except Exception as exc:  # pragma: no cover - exercised in real fetch runs
            failures.append((event.event_id, str(exc)))
            console.print(f"[red]  Failed: {exc}[/red]")
            continue

        written = sum(len(result.files) for result in fetch_results)
        total_files += written
        if written == 0:
            console.print("[yellow]  No VIIRS files found for this case[/yellow]")
            continue

        console.print(f"[bold]  Wrote {written} files[/bold]")

    console.print(f"\n[bold]Total files written:[/bold] {total_files}")
    if failures:
        for failed_case, message in failures:
            console.print(f"[red]- {failed_case}: {message}[/red]")
        raise typer.Exit(code=1)


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
