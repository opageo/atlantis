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
from atlantis.utils.kurosiwo import (
    KUROSIWO_DEFAULT_CATALOGUE,
    KUROSIWO_DEFAULT_METADATA,
    build_kurosiwo_flood_events,
    build_kurosiwo_flood_events_from_catalogue,
    write_kurosiwo_metadata_csv,
)
from atlantis.utils.plot import (
    date_from_filename,
    pixel_stats_classified,
    pixel_stats_raw,
    plot_classified,
    plot_raw,
)

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
    viirs_backend: str = typer.Option(
        "noaa_s3",
        "--viirs-backend",
        help="VIIRS backend: noaa_s3 or gmu_legacy",
    ),
    viirs_format: str = typer.Option(
        "tif",
        "--viirs-format",
        help="VIIRS format: tif, netcdf, shapezip, png. Only tif is implemented.",
    ),
    classify: bool = typer.Option(
        False,
        "--classify",
        help="Classify VIIRS pixels into flood-extent, quality-mask, and permanent-water"
        " layers instead of writing raw data.",
    ),
    stream: bool = typer.Option(
        False,
        "--stream",
        help="Stream remote tiles via GDAL /vsicurl/ without downloading to disk"
        " (saves storage, requires network during processing).",
    ),
    flood_threshold: int = typer.Option(
        160,
        "--flood-threshold",
        min=101,
        max=200,
        help="Minimum VIIRS pixel code for flood classification (101–200). "
        "Default: 160 (60%+ water). 101=all flood. 200=most conservative.",
    ),
    plot: bool = typer.Option(
        False,
        "--plot",
        help="Save a PNG visualisation of the peak-flood date (VIIRS only).",
    ),
    plot_dir: Path | None = typer.Option(
        None,
        "--plot-dir",
        help="Directory to write PNG files (default: <output>/plots/).",
    ),
    harmonise: bool = typer.Option(
        False,
        "--harmonise",
        help="Harmonise the peak-flood date to 1 arcmin after fetching (VIIRS only).",
    ),
) -> None:
    """Fetch raw inundation data from specified source(s).

    Args:
        event: Flood event ID to fetch data for.
        source: Data source to fetch from. Options: gfm, viirs, rfm, all.
        output_dir: Directory to save downloaded files.
        bbox: Bounding box as west south east north for direct event construction.
        start_date: Start date for direct event construction in YYYY-MM-DD format.
        end_date: End date for direct event construction in YYYY-MM-DD format.
        viirs_backend: Which VIIRS backend to use (noaa_s3 or gmu_legacy).
        viirs_format: Which VIIRS data format to fetch (tif, netcdf, shapezip, png). Only tif is implemented.
        classify: If True, write flood-extent/quality-mask/permanent-water layers instead of raw data.
        stream: If True, stream remote tiles without downloading to disk.
        flood_threshold: Minimum VIIRS pixel code for flood (101–200, default 160).
        plot: Save PNG visualisation of the peak-flood date (VIIRS only).
        plot_dir: Directory for PNG output (default: <output>/plots/).
        harmonise: Harmonise the peak-flood date to 1 arcmin (VIIRS only).
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
            fetcher_kwargs = {}
            if src == "viirs":
                fetcher_kwargs = {
                    "backend": viirs_backend,
                    "data_format": viirs_format,
                    "classify": classify,
                    "stream": stream,
                    "flood_min_code": flood_threshold,
                }
            fetcher = fetcher_cls(**fetcher_kwargs)
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

            # ── Optional plot + harmonise (VIIRS only) ────────────────────
            if src == "viirs" and (plot or harmonise):
                best_result = None
                best_date_label = ""
                best_flood_count = 0
                for result in fetch_results:
                    ds = fetcher.to_dataset(result)
                    date_label = date_from_filename(result.files[0].name)
                    if "flood_extent" in ds:
                        flooded = pixel_stats_classified(ds["flood_extent"].values, name=date_label)
                        if flooded > best_flood_count:
                            best_flood_count = flooded
                            best_result = result
                            best_date_label = date_label
                    else:
                        pixel_stats_raw(ds["raw"].values, name=date_label)
                if best_result is None:
                    best_result = fetch_results[0]
                    best_date_label = date_from_filename(fetch_results[0].files[0].name)
                best_ds = fetcher.to_dataset(best_result)
                if plot:
                    png_out = (plot_dir or (output_dir / src / "plots")) / f"{event}_{best_date_label}_viirs.png"
                    if "flood_extent" in best_ds:
                        plot_classified(
                            best_ds["flood_extent"],
                            title=f"{event}: VIIRS flood extent {best_date_label} (375 m)",
                            output_path=png_out,
                        )
                    else:
                        plot_raw(
                            best_ds["raw"],
                            title=f"{event}: VIIRS raw composite {best_date_label} (375 m)",
                            output_path=png_out,
                        )
                if harmonise:
                    from atlantis.harmoniser import Harmoniser

                    harm_dir = output_dir / src / "harmonised"
                    harm_dir.mkdir(parents=True, exist_ok=True)
                    h = Harmoniser()
                    ds_harm = h.harmonise(best_ds, source_id="viirs")
                    flood_var = "flood_extent" if "flood_extent" in ds_harm else list(ds_harm.data_vars)[0]
                    tif_path = harm_dir / f"{event}_{best_date_label}_viirs_harmonised.tif"
                    ds_harm[flood_var].rio.to_raster(
                        str(tif_path), dtype="float32", compress="LZW", nodata=float("nan")
                    )
                    console.print(f"  Harmonised → {tif_path.name}")
        except KeyError:
            console.print(f"[red]Error: Unknown source '{src}'[/red]")


@cli.command("fetch-kurosiwo-viirs")
def fetch_kurosiwo_viirs(
    metadata_path: Path | None = typer.Option(
        None,
        "--metadata",
        help="Path to precomputed KuroSiwo metadata CSV",
    ),
    catalogue_path: Path = typer.Option(
        KUROSIWO_DEFAULT_CATALOGUE,
        "--catalogue",
        help="Path to the KuroSiwo GeoPackage catalogue used when metadata CSV is not supplied",
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
    viirs_backend: str = typer.Option(
        "noaa_s3",
        "--viirs-backend",
        help="VIIRS backend: noaa_s3 or gmu_legacy",
    ),
    viirs_format: str = typer.Option(
        "tif",
        "--viirs-format",
        help="VIIRS format: tif, netcdf, shapezip, png. Only tif is implemented.",
    ),
    classify: bool = typer.Option(
        False,
        "--classify",
        help="Classify VIIRS pixels into flood-extent, quality-mask, and permanent-water"
        " layers instead of writing raw data.",
    ),
    stream: bool = typer.Option(
        False,
        "--stream",
        help="Stream remote tiles via GDAL /vsicurl/ without downloading to disk.",
    ),
    flood_threshold: int = typer.Option(
        160,
        "--flood-threshold",
        min=101,
        max=200,
        help="Minimum VIIRS pixel code for flood classification (101–200). "
        "Default: 160 (60%+ water). 101=all flood. 200=most conservative.",
    ),
    plot: bool = typer.Option(
        False,
        "--plot",
        help="Save a PNG visualisation of the peak-flood date for each case.",
    ),
    plot_dir: Path | None = typer.Option(
        None,
        "--plot-dir",
        help="Directory to write PNG files (default: <output>/plots/).",
    ),
    harmonise: bool = typer.Option(
        False,
        "--harmonise",
        help="Harmonise the peak-flood date to 1 arcmin and write a GeoTIFF alongside the fetch output.",
    ),
) -> None:
    """Fetch VIIRS data for KuroSiwo cases.

    Args:
        metadata_path: Optional precomputed metadata CSV path.
        catalogue_path: KuroSiwo GeoPackage catalogue path used when metadata CSV is omitted.
        case: Only fetch one KuroSiwo flood case.
        limit: Limit the number of cases after filtering.
        output_dir: Output directory for VIIRS products.
        days_before: Days before the KuroSiwo flood date to search.
        days_after: Days after the KuroSiwo flood date to search.
        use_metadata_range: Use the full metadata temporal range instead of a narrow flood-date window.
        viirs_backend: Which VIIRS backend to use (noaa_s3 or gmu_legacy).
        viirs_format: Which VIIRS data format to fetch (tif, netcdf, shapezip, png). Only tif is implemented.
        classify: If True, write flood-extent/quality-mask/permanent-water layers instead of raw data.
        stream: If True, stream remote tiles without downloading to disk.
        flood_threshold: Minimum VIIRS pixel code for flood (101–200, default 160).
        plot: Save PNG visualisation of the peak-flood date per case.
        plot_dir: Directory for PNG output (default: <output>/plots/).
        harmonise: Harmonise the peak-flood date to 1 arcmin.
    """
    config = get_config()
    output_root = output_dir or config.fetcher.cache_dir / "raw" / "kurosiwo"
    output_root.mkdir(parents=True, exist_ok=True)

    if metadata_path is not None:
        events = build_kurosiwo_flood_events(
            metadata_path,
            case=case,
            limit=limit,
            days_before=days_before,
            days_after=days_after,
            use_metadata_range=use_metadata_range,
        )
        metadata_source_label = str(metadata_path)
    else:
        events = build_kurosiwo_flood_events_from_catalogue(
            catalogue_path,
            case=case,
            limit=limit,
            days_before=days_before,
            days_after=days_after,
            use_metadata_range=use_metadata_range,
        )
        metadata_source_label = f"derived from {catalogue_path}"

    fetcher_cls = get_fetcher("viirs")
    fetcher = fetcher_cls(
        backend=viirs_backend,
        data_format=viirs_format,
        classify=classify,
        stream=stream,
        flood_min_code=flood_threshold,
    )

    console.print(f"[bold]KuroSiwo metadata:[/bold] {metadata_source_label}")
    console.print(f"[bold]Cases selected:[/bold] {len(events)}")
    console.print(f"[bold]Output root:[/bold] {output_root}")

    total_files = 0
    failures: list[tuple[str, str]] = []

    for event in events:
        console.print(
            f"\n[cyan]Fetching {event.event_id}[/cyan] ({event.start_date.isoformat()} -> {event.end_date.isoformat()})"
        )
        event_viirs_dir = output_root / event.event_id / "viirs"
        try:
            fetch_results = fetcher.fetch(event, event_viirs_dir)
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

        # ── Per-date stats + best-date selection ──────────────────────────
        if plot or harmonise:
            best_result = None
            best_date_label = ""
            best_flood_count = 0

            for result in fetch_results:
                ds = fetcher.to_dataset(result)
                date_label = date_from_filename(result.files[0].name)
                console.print(f"  {date_label}", end="")

                if "flood_extent" in ds:
                    flooded = pixel_stats_classified(ds["flood_extent"].values, name="")
                    if flooded > best_flood_count:
                        best_flood_count = flooded
                        best_result = result
                        best_date_label = date_label
                else:
                    pixel_stats_raw(ds["raw"].values, name="")

            if best_result is None:
                best_result = fetch_results[0]
                best_date_label = date_from_filename(fetch_results[0].files[0].name)

            best_ds = fetcher.to_dataset(best_result)

            if plot:
                png_dir = plot_dir or (event_viirs_dir / "plots")
                png_path = png_dir / f"{event.event_id}_{best_date_label}_viirs.png"
                if "flood_extent" in best_ds:
                    plot_classified(
                        best_ds["flood_extent"],
                        title=f"{event.event_id}: VIIRS flood extent {best_date_label} (375 m)",
                        output_path=png_path,
                    )
                else:
                    plot_raw(
                        best_ds["raw"],
                        title=f"{event.event_id}: VIIRS raw composite {best_date_label} (375 m)",
                        output_path=png_path,
                    )

            if harmonise:
                from atlantis.harmoniser import Harmoniser

                harm_dir = event_viirs_dir / "harmonised"
                harm_dir.mkdir(parents=True, exist_ok=True)
                h = Harmoniser()
                ds_harm = h.harmonise(best_ds, source_id="viirs")
                flood_var = "flood_extent" if "flood_extent" in ds_harm else list(ds_harm.data_vars)[0]
                tif_path = harm_dir / f"{event.event_id}_{best_date_label}_viirs_harmonised.tif"
                ds_harm[flood_var].rio.to_raster(str(tif_path), dtype="float32", compress="LZW", nodata=float("nan"))
                console.print(f"  Harmonised → {tif_path.name}")

    console.print(f"\n[bold]Total files written:[/bold] {total_files}")
    if failures:
        for failed_case, message in failures:
            console.print(f"[red]- {failed_case}: {message}[/red]")
        raise typer.Exit(code=1)


@cli.command("build-kurosiwo-metadata")
def build_kurosiwo_metadata(
    catalogue_path: Path = typer.Option(
        KUROSIWO_DEFAULT_CATALOGUE,
        "--catalogue",
        help="Path to the KuroSiwo GeoPackage catalogue",
    ),
    output_path: Path = typer.Option(
        KUROSIWO_DEFAULT_METADATA,
        "--output",
        help="Path to the output metadata CSV",
    ),
) -> None:
    """Derive the KuroSiwo metadata CSV from the catalogue.

    Args:
        catalogue_path: Path to the KuroSiwo GeoPackage catalogue.
        output_path: Destination path for the derived metadata CSV.
    """
    written_path = write_kurosiwo_metadata_csv(catalogue_path, output_path)
    console.print(f"[bold]KuroSiwo catalogue:[/bold] {catalogue_path}")
    console.print(f"[bold]Metadata CSV written:[/bold] {written_path}")


@cli.command()
def harmonise(
    event: str = typer.Option(..., "--event", "-e", help="Flood event ID"),
    source: str = typer.Option(..., "--source", "-s", help="Data source ID"),
    input_dir: Path | None = typer.Option(None, "--input", "-i", help="Input directory with fetched data"),
    output_dir: Path | None = typer.Option(None, "--output", "-o", help="Output directory for harmonised data"),
    target_resolution: float | None = typer.Option(
        None,
        "--target-resolution",
        help="Target spatial resolution in degrees (default: 0.01667 = 1 arcmin)",
    ),
    resampling: str | None = typer.Option(
        None,
        "--resampling",
        help="Resampling method for flood_extent (default: average)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be done without doing it"),
) -> None:
    """Harmonise fetched VIIRS data (reproject + normalise) to target resolution.

    Reads processed GeoTIFFs from the fetcher output directory, reprojects
    them to a uniform 1 arcmin grid, normalises flood extent values to 0-1,
    and writes the harmonised GeoTIFFs.
    """
    from atlantis.harmoniser import Harmoniser

    config = get_config()
    input_root = input_dir or config.fetcher.cache_dir / "raw" / event
    output_root = output_dir or config.fetcher.cache_dir / "harmonised" / event

    if not input_root.exists():
        console.print(f"[yellow]Default input not found: {input_root}[/yellow]")

    # ── Build harmoniser with optional overrides ──────────────────────
    if target_resolution is not None or resampling is not None:
        cfg = HarmoniseConfig()
        if target_resolution is not None:
            cfg.target_resolution = target_resolution
            cfg.target_resolution_arcmin = round(target_resolution * 60, 4)
        if resampling is not None:
            cfg.resampling = resampling  # type: ignore[assignment]
            if source == "viirs":
                cfg.variable_resampling["flood_extent"] = resampling
        harmoniser = Harmoniser(config=cfg)
    else:
        harmoniser = Harmoniser()

    # ── Find processed files ──────────────────────────────────────────
    import rioxarray as rxr

    processed_dir: Path | None = None
    tif_files: list[Path] = []

    # Search strategy: try the standard layout first, then rglob broadly
    for root in (input_root, Path.cwd() / "scripts" / "data", Path.cwd()):
        if not root.exists():
            continue
        # Try root/<source>/processed/ first
        candidate = root / source / "processed"
        if candidate.exists():
            hits = sorted(candidate.glob(f"{event}_*_viirs_flood_extent.tif"))
            if not hits:
                hits = sorted(candidate.glob(f"{event}_*_viirs_raw.tif"))
            if hits:
                processed_dir, tif_files = candidate, hits
                break
        # Fallback: check root directly (files may be flat)
        hits = sorted(root.glob(f"{event}_*_viirs_flood_extent.tif"))
        if not hits:
            hits = sorted(root.glob(f"{event}_*_viirs_raw.tif"))
        if hits:
            processed_dir, tif_files = root, hits
            break

        # KuroSiwo deep pattern: <root>/<case>/viirs/processed/<case>_<date>_*.tif
        for vdir in sorted(root.rglob("viirs/processed/")):
            hits = sorted(vdir.glob(f"{event}_*_viirs_flood_extent.tif"))
            if not hits:
                hits = sorted(vdir.glob(f"{event}_*_viirs_raw.tif"))
            if hits:
                processed_dir, tif_files = vdir, hits
                break
        if tif_files:
            break

    if not tif_files:
        console.print(f"[red]No processed VIIRS files found matching '{event}'[/red]")
        console.print("  Tried: cache dir, scripts/data/, and repository root.")
        console.print("  Run 'atlantis fetch' first, or use --input to point to existing data.")
        raise typer.Exit(code=1)

    output_path = output_root / source / "harmonised"
    output_path.mkdir(parents=True, exist_ok=True)

    resolution_str = f"{harmoniser.config.target_resolution_arcmin} arcmin"
    console.print(f"[bold]Harmonising {len(tif_files)} file(s)[/bold]")
    console.print(f"[bold]Input:[/bold] {processed_dir}")
    console.print(f"[bold]Output:[/bold] {output_path}")
    console.print(f"[bold]Target resolution:[/bold] {resolution_str} ({harmoniser.config.target_resolution:.8f}°)")
    console.print(f"[bold]Resampling:[/bold] {harmoniser.config.resampling}")

    if dry_run:
        for tf in tif_files:
            stem = tf.stem.replace("flood_extent", "harmonised").replace("raw", "harmonised")
            out = output_path / f"{stem}.tif"
            console.print(f"  Would process: {tf.name} → {out.name}")
        return

    harmonised_count = 0
    for tif_path in tif_files:
        # Determine if this is flood_extent, raw, or quality_mask
        input_var = "flood_extent" if "flood_extent" in tif_path.name else "raw"
        stem = tif_path.stem.replace("flood_extent", "harmonised").replace("raw", "harmonised")
        out_path = output_path / f"{stem}.tif"

        console.print(f"  Processing: {tif_path.name} ...", end="")
        ds = rxr.open_rasterio(tif_path).squeeze(drop=True).to_dataset(name=input_var)
        ds_harmonised = harmoniser.harmonise(ds, source_id=source, flood_variable=input_var)

        flood_var = input_var if input_var in ds_harmonised.data_vars else "flood_extent"
        ds_harmonised[flood_var].rio.to_raster(
            str(out_path),
            dtype="float32",
            compress="LZW",
            nodata=float("nan"),
        )
        harmonised_count += 1
        console.print(f" done → {out_path.name}")

    console.print(f"\n[bold]Wrote {harmonised_count} harmonised file(s) to {output_path}[/bold]")


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
