"""CLI entrypoints for Atlantis."""

from datetime import date
from pathlib import Path

import typer
from rasterio.enums import Resampling
from rich.console import Console

from atlantis.config import HarmoniseConfig, get_config

# Import fetchers to register them
from atlantis.fetchers import fetcher_registry, get_fetcher, list_fetchers, rfm, viirs  # noqa: F401
from atlantis.fetchers.base import FetchResult
from atlantis.harmoniser import write_harmonised_raster
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
from atlantis.utils.ui import (
    command_header,
    console,
    fail,
    file_tree,
    info,
    make_progress,
    ok,
    section_rule,
    step_status,
    summary_table,
    warn,
)

cli = typer.Typer(help="Atlantis — ML-ready flood inundation archive pipeline.", pretty_exceptions_enable=False)
console = Console()


# ── Shared plot + harmonise helper ──────────────────────────────────────────


def _viirs_date_label(result: FetchResult) -> str:
    """Human-readable date label for a VIIRS fetch result."""
    if result.date_token:
        token = result.date_token
        # Only format as YYYY-MM-DD if the token is an 8-digit date string.
        # Special tokens like "aggregated" are returned as-is.
        if len(token) == 8 and token.isdigit():
            return f"{token[:4]}-{token[4:6]}-{token[6:8]}"
        return token
    if result.files:
        return date_from_filename(result.files[0].name)
    return "unknown"


def _report_viirs_fetch_writes(fetch_results: list[FetchResult], *, keep_processed: bool) -> None:
    """Print what the VIIRS fetcher persisted (disk vs in-memory composite/peak date)."""
    disk_files = sum(len(result.files) for result in fetch_results)
    if disk_files:
        ok(f"Wrote {disk_files} files")
        all_paths = [path for result in fetch_results for path in result.files]
        console.print(file_tree("viirs", all_paths))
        return
    if fetch_results and not keep_processed:
        label = _viirs_date_label(fetch_results[0])
        if label == "aggregated":
            info("Aggregated composite: processed in memory (no processed/ GeoTIFFs)")
        else:
            info(f"Peak-flood date {label}: processed in memory (no processed/ GeoTIFFs)")


def _report_empty_fetch(source_id: str, fetcher) -> None:
    """Explain why a fetcher returned no results, using diagnostics when available.

    Generic fetchers fall back to the previous one-line message; VIIRS exposes
    structured diagnostics via :attr:`VIIRSFetcher.last_diagnostics` and we
    translate them into actionable guidance here.
    """
    diagnostics = getattr(fetcher, "last_diagnostics", None)
    if diagnostics is None:
        warn("No files were fetched")
        return

    warn("No files were fetched.")
    if diagnostics.missing_aoi_coverage:
        warn("Reason: event bbox does not intersect any packaged VIIRS AOI.")
        info("Hint: VIIRS AOIs cover ±60° latitude on a fixed global grid. Widen the bbox or verify the coordinates.")
        return

    if diagnostics.year_coverage_gap:
        published = (
            ", ".join(str(y) for y in sorted(diagnostics.available_years)) if diagnostics.available_years else "unknown"
        )
        requested = ", ".join(str(y) for y in sorted(diagnostics.requested_years))
        warn(f"Reason: backend '{diagnostics.backend}' does not publish data for the requested year(s) ({requested}).")
        info(f"Published years on this backend: {published}")
        if diagnostics.backend == "noaa_s3":
            info(
                "Hint: try --viirs-backend gmu_legacy for legacy years (2021–2022), or pick "
                "an event in a published year."
            )
        return

    if diagnostics.listings_all_empty:
        warn(
            f"Reason: backend '{diagnostics.backend}' returned no listings for any of "
            f"the {diagnostics.dates_probed} requested date(s)."
        )
        info(
            "Hint: the bucket is up but the daily prefixes are empty for this window. Try "
            "broadening --start-date/--end-date or switching --viirs-backend."
        )
        return

    if diagnostics.no_aoi_match_in_listings:
        warn(
            f"Reason: {diagnostics.dates_with_listings} date(s) had listings, but none "
            f"contained tiles for the {diagnostics.aoi_count} intersecting AOI(s)."
        )
        info("Hint: the AOIs intersecting this bbox were not produced for the requested dates.")
        return

    warn(
        f"Reason: backend '{diagnostics.backend}' produced no processable tiles "
        f"({diagnostics.result_count} search results across {diagnostics.dates_probed} date(s))."
    )


def _select_best_result(
    fetcher,
    fetch_results,
):
    """Select the fetch result with the highest flood pixel count."""
    if len(fetch_results) == 1:
        return fetch_results[0], _viirs_date_label(fetch_results[0])

    best_result = None
    best_date_label = ""
    best_flood_count = 0

    for result in fetch_results:
        ds = fetcher.to_dataset(result)
        date_label = _viirs_date_label(result)
        if "flood_fraction" in ds:
            flooded = pixel_stats_classified(ds["flood_fraction"].values, name=date_label)
            if flooded > best_flood_count:
                best_flood_count = flooded
                best_result = result
                best_date_label = date_label
        else:
            pixel_stats_raw(ds["raw"].values, name=date_label)

    if best_result is None:
        best_result = fetch_results[0]
        best_date_label = _viirs_date_label(fetch_results[0])

    return best_result, best_date_label


def _plot_viirs(
    best_ds,
    event_id,
    date_label,
    *,
    output_png_path,
):
    """Save a PNG visualisation of the VIIRS peak-flood date."""
    if "flood_fraction" in best_ds:
        plot_classified(
            best_ds["flood_fraction"],
            title=f"{event_id}: VIIRS flood extent {date_label} (375 m)",
            output_path=output_png_path,
        )
    else:
        plot_raw(
            best_ds["raw"],
            title=f"{event_id}: VIIRS raw composite {date_label} (375 m)",
            output_path=output_png_path,
        )


def _harmonise_viirs(
    best_ds,
    event_id,
    date_label,
    *,
    harm_dir,
    plot_dir,
):
    """Reproject + normalise and save harmonised GeoTIFF (in ``harm_dir``) + PNG (in ``plot_dir``).

    Returns the xarray Dataset produced by the harmoniser for downstream use.
    """
    from atlantis.harmoniser import Harmoniser

    harm_dir.mkdir(parents=True, exist_ok=True)
    plot_dir.mkdir(parents=True, exist_ok=True)
    h = Harmoniser()
    ds_harm = h.harmonise(best_ds, source_id="viirs")
    flood_var = "flood_fraction" if "flood_fraction" in ds_harm else list(ds_harm.data_vars)[0]

    tif_path = harm_dir / f"{event_id}_{date_label}_viirs_harmonised.tif"
    write_harmonised_raster(ds_harm[flood_var], tif_path)
    console.print(f"  Harmonised → {tif_path.name}")

    png_harm_path = plot_dir / f"{event_id}_{date_label}_viirs_harmonised.png"
    if flood_var == "flood_fraction":
        plot_classified(
            ds_harm[flood_var],
            title=f"{event_id}: VIIRS harmonised flood extent {date_label} (1 arcmin)",
            output_path=png_harm_path,
        )
    else:
        plot_raw(
            ds_harm[flood_var],
            title=f"{event_id}: VIIRS harmonised composite {date_label} (1 arcmin)",
            output_path=png_harm_path,
        )

    return ds_harm


def _report_gfm_fetch(fetch_results: list[FetchResult]) -> None:
    """Print summary of GFM fetch results."""
    n_results = len(fetch_results)
    dates = [r.date_token or "unknown" for r in fetch_results]
    console.print(f"[bold]  Processed {n_results} GFM result(s): dates={', '.join(dates)}[/bold]")
    for result in fetch_results:
        if result.files:
            for path in result.files:
                console.print(f"  - {path}")
        elif result.dataset is not None:
            console.print(f"  - In-memory dataset (date={result.date_token})")


def _plot_gfm(
    ds,
    event_id,
    date_label,
    *,
    output_png_path,
):
    """Save a PNG visualisation of GFM flood extent."""
    output_png_path = Path(output_png_path)
    output_png_path.parent.mkdir(parents=True, exist_ok=True)
    if "flood_fraction" in ds:
        plot_classified(
            ds["flood_fraction"],
            title=f"{event_id}: GFM flood extent {date_label}",
            output_path=output_png_path,
        )
    console.print(f"  Plot → {output_png_path.name}")


def _harmonise_gfm(
    ds,
    event_id,
    date_label,
    *,
    harm_dir,
):
    """Harmonise GFM data and save GeoTIFF + PNG."""
    from atlantis.harmoniser import Harmoniser

    harm_dir.mkdir(parents=True, exist_ok=True)
    h = Harmoniser()
    ds_harm = h.harmonise(ds, source_id="gfm", flood_variable="flood_fraction")

    tif_path = harm_dir / f"{event_id}_{date_label}_gfm_harmonised.tif"
    write_harmonised_raster(ds_harm["flood_fraction"], tif_path)
    console.print(f"  Harmonised → {tif_path.name}")

    png_harm_path = harm_dir / f"{event_id}_{date_label}_gfm_harmonised.png"
    plot_classified(
        ds_harm["flood_fraction"],
        title=f"{event_id}: GFM harmonised flood extent {date_label} (1 arcmin)",
        output_path=png_harm_path,
    )
    return ds_harm


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
        True,
        "--classify/--no-classify",
        help="Classify VIIRS pixels into flood-extent, quality-mask, and permanent-water"
        " layers instead of writing raw data. Default: on."
        " Use --no-classify to write raw integer pixel codes instead.",
    ),
    stream: bool = typer.Option(
        True,
        "--stream/--no-stream",
        help="Stream remote tiles via GDAL /vsicurl/ without downloading to disk"
        " (saves storage, requires network during processing). Default: on."
        " Use --no-stream to download tiles to disk instead.",
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
    strategy: str = typer.Option(
        "peak",
        "--strategy",
        help="How to handle multiple dates: peak (best flood date), aggregate (mean/mode), all (every date)",
    ),
    keep_processed: bool = typer.Option(
        True,
        "--keep-processed/--no-keep-processed",
        help="Write intermediate processed/ GeoTIFFs. Use --no-keep-processed to save disk space.",
    ),
    gfm_coarsen_factor: int = typer.Option(
        4,
        "--gfm-coarsen-factor",
        help="GFM spatial coarsening factor before reprojection (default 4).",
    ),
    _gfm_resampling: str = typer.Option(
        "average",
        "--gfm-resampling",
        help="GFM resampling method for reprojection to EPSG:4326.",
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
        classify: If True, write flood-fraction/quality-mask/permanent-water layers instead of raw data.
        stream: If True, stream remote tiles without downloading to disk.
        plot: Save PNG visualisation of the peak-flood date (VIIRS only).
        plot_dir: Directory for PNG output (default: <output>/plots/).
        harmonise: Harmonise the peak-flood date to 1 arcmin (VIIRS only).
        strategy: How to handle multiple dates: peak, aggregate, all.
        keep_processed: Write intermediate processed/ GeoTIFFs.
    """
    config = get_config()
    output_dir = output_dir or config.fetcher.cache_dir / "raw" / event
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        gfm_resampling = Resampling[_gfm_resampling]
    except KeyError:
        valid = [r.name for r in Resampling]
        raise typer.BadParameter(f"--gfm-resampling '{_gfm_resampling}' is not valid. Choose from: {', '.join(valid)}")
    if source is None or source == "all":
        sources = list_fetchers()
    else:
        sources = [source]

    command_header("fetch", subtitle=f"{event} · sources={', '.join(sources)}")
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
            section_rule(src)
            fetcher_kwargs = {}
            if src == "viirs":
                fetcher_kwargs = {
                    "backend": viirs_backend,
                    "data_format": viirs_format,
                    "classify": classify,
                    "stream": stream,
                    "strategy": strategy,
                    "keep_processed": keep_processed,
                }
            elif src == "gfm":
                fetcher_kwargs = {
                    "coarsen_factor": gfm_coarsen_factor,
                    "resampling": gfm_resampling,
                    "strategy": strategy,
                    "keep_processed": keep_processed,
                }
            fetcher = fetcher_cls(**fetcher_kwargs)
            if flood_event is None:
                warn("Event catalogue lookup not yet implemented; provide --bbox/--start-date/--end-date")
                continue

            with step_status(f"Fetching {src} tiles…"):
                fetch_results = fetcher.fetch(flood_event, output_dir / src)
            if not fetch_results:
                _report_empty_fetch(src, fetcher)
                continue

            if src == "viirs":
                _report_viirs_fetch_writes(fetch_results, keep_processed=keep_processed)
            else:
                n = sum(len(result.files) for result in fetch_results)
                ok(f"Wrote {n} files")
                all_paths = [path for result in fetch_results for path in result.files]
                console.print(file_tree(src, all_paths))

            # ── Optional plot + harmonise (GFM) ─────────────────────────
            if src == "gfm" and (plot or harmonise):
                for result in fetch_results:
                    ds = fetcher.to_dataset(result)
                    date_label = result.date_token or "gfm"
                    if plot:
                        png_out = (plot_dir or (output_dir / src / "plots")) / f"{event}_{date_label}_gfm.png"
                        _plot_gfm(ds, event, date_label, output_png_path=png_out)
                    if harmonise:
                        harm_dir = output_dir / src / "harmonised"
                        _harmonise_gfm(ds, event, date_label, harm_dir=harm_dir)

            # ── Optional plot + harmonise (VIIRS only) ────────────────────
            if src == "viirs" and (plot or harmonise):
                # Dispatch based on strategy
                if strategy == "peak":
                    best_result, best_date_label = _select_best_result(fetcher, fetch_results)
                    best_ds = fetcher.to_dataset(best_result)
                    png_dir = plot_dir or (output_dir / src / "plots")
                    if plot:
                        png_out = png_dir / f"{event}_{best_date_label}_viirs.png"
                        with step_status("Plotting…"):
                            _plot_viirs(best_ds, event, best_date_label, output_png_path=png_out)
                    if harmonise:
                        harm_dir = output_dir / src / "harmonised"
                        with step_status("Harmonising…"):
                            _harmonise_viirs(best_ds, event, best_date_label, harm_dir=harm_dir, plot_dir=png_dir)

                elif strategy == "aggregate":
                    ds = fetcher.to_dataset(fetch_results[0])
                    label = "aggregated"
                    png_dir = plot_dir or (output_dir / src / "plots")
                    if plot:
                        png_out = png_dir / f"{event}_{label}_viirs.png"
                        with step_status("Plotting…"):
                            _plot_viirs(ds, event, label, output_png_path=png_out)
                    if harmonise:
                        harm_dir = output_dir / src / "harmonised"
                        with step_status("Harmonising…"):
                            _harmonise_viirs(ds, event, label, harm_dir=harm_dir, plot_dir=png_dir)

                elif strategy == "all":
                    for result in fetch_results:
                        date_label = _viirs_date_label(result)
                        ds = fetcher.to_dataset(result)
                        png_dir = plot_dir or (output_dir / src / "plots")
                        if plot:
                            png_out = png_dir / f"{event}_{date_label}_viirs.png"
                            with step_status(f"Plotting {date_label}…"):
                                _plot_viirs(ds, event, date_label, output_png_path=png_out)
                        if harmonise:
                            harm_dir = output_dir / src / "harmonised"
                            with step_status(f"Harmonising {date_label}…"):
                                _harmonise_viirs(ds, event, date_label, harm_dir=harm_dir, plot_dir=png_dir)
        except KeyError:
            fail(f"Unknown source '{src}'")


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
        True,
        "--classify/--no-classify",
        help="Classify VIIRS pixels into flood-extent, quality-mask, and permanent-water"
        " layers instead of writing raw data. Default: on."
        " Use --no-classify to write raw integer pixel codes instead.",
    ),
    stream: bool = typer.Option(
        True,
        "--stream/--no-stream",
        help="Stream remote tiles via GDAL /vsicurl/ without downloading to disk."
        " Default: on. Use --no-stream to download tiles to disk instead.",
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
    keep_processed: bool = typer.Option(
        True,
        "--keep-processed/--no-keep-processed",
        help="Write intermediate processed/ GeoTIFFs. Use --no-keep-processed to save disk space.",
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
        classify: If True, write flood-fraction/quality-mask/permanent-water layers instead of raw data.
        stream: If True, stream remote tiles without downloading to disk.
        plot: Save PNG visualisation of the peak-flood date per case.
        plot_dir: Directory for PNG output (default: <output>/plots/).
        harmonise: Harmonise the peak-flood date to 1 arcmin.
        keep_processed: Write intermediate processed/ GeoTIFFs.
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
        keep_processed=keep_processed,
    )

    command_header(
        "fetch-kurosiwo-viirs",
        subtitle=f"{len(events)} case(s) · backend={viirs_backend}",
    )
    console.print(f"[bold]KuroSiwo metadata:[/bold] {metadata_source_label}")
    console.print(f"[bold]Cases selected:[/bold] {len(events)}")
    console.print(f"[bold]Output root:[/bold] {output_root}")

    total_files = 0
    failures: list[tuple[str, str]] = []
    # summary rows: [case, status, files, peak_date, harmonised]
    summary_rows: list[list[str]] = []

    actual_harmonise = harmonise

    with make_progress() as progress:
        task = progress.add_task("[cyan]Cases[/cyan]", total=len(events))

        for event in events:
            progress.console.print(
                f"\n[cyan]Fetching {event.event_id}[/cyan] "
                f"({event.start_date.isoformat()} → {event.end_date.isoformat()})"
            )
            event_viirs_dir = output_root / event.event_id / "viirs"
            try:
                with step_status(f"  Fetching tiles for {event.event_id}…"):
                    fetch_results = fetcher.fetch(event, event_viirs_dir)
            except Exception as exc:  # pragma: no cover - exercised in real fetch runs
                failures.append((event.event_id, str(exc)))
                progress.console.print(f"[bold red]✗[/bold red]  Failed: {exc}")
                summary_rows.append([event.event_id, "[red]✗ failed[/red]", "0", "—", "—"])
                progress.advance(task)
                continue

            _report_viirs_fetch_writes(fetch_results, keep_processed=keep_processed)
            written = sum(len(result.files) for result in fetch_results)
            total_files += written
            has_in_memory = any(result.dataset is not None for result in fetch_results)
            if written == 0 and not has_in_memory:
                warn("No VIIRS files found for this case")
                summary_rows.append([event.event_id, "[yellow]⚠ empty[/yellow]", "0", "—", "—"])
                progress.advance(task)
                continue

            peak_label = "—"
            harmonised_label = "—"

            # ── Per-date stats + best-date selection ──────────────────────────
            if plot or actual_harmonise:
                best_result, best_date_label = _select_best_result(fetcher, fetch_results)
                best_ds = fetcher.to_dataset(best_result)
                peak_label = best_date_label
                png_dir = plot_dir or (event_viirs_dir / "plots")

                if plot:
                    png_path = png_dir / f"{event.event_id}_{best_date_label}_viirs.png"
                    with step_status(f"  Plotting {best_date_label}…"):
                        _plot_viirs(best_ds, event.event_id, best_date_label, output_png_path=png_path)

                if actual_harmonise:
                    harm_dir = event_viirs_dir / "harmonised"
                    with step_status(f"  Harmonising {best_date_label}…"):
                        _harmonise_viirs(best_ds, event.event_id, best_date_label, harm_dir=harm_dir, plot_dir=png_dir)
                    harmonised_label = "✓"

            summary_rows.append(
                [
                    event.event_id,
                    "[green]✓ ok[/green]",
                    str(written) if written else ("mem" if has_in_memory else "0"),
                    peak_label,
                    harmonised_label,
                ]
            )
            progress.advance(task)

    console.print(f"\n[bold]Total files written:[/bold] {total_files}")
    if summary_rows:
        console.print(
            summary_table(
                "KuroSiwo VIIRS — run summary",
                ["Case", "Status", "Files", "Peak date", "Harmonised"],
                summary_rows,
            )
        )
    if failures:
        for failed_case, message in failures:
            fail(f"{failed_case}: {message}")
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
    command_header("build-kurosiwo-metadata")
    console.print(f"[bold]KuroSiwo catalogue:[/bold] {catalogue_path}")
    ok(f"Metadata CSV written: {written_path}")


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
        help="Resampling method for flood_fraction (default: average)",
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
        warn(f"Default input not found: {input_root}")

    # ── Build harmoniser with optional overrides ──────────────────────
    if target_resolution is not None or resampling is not None:
        cfg = HarmoniseConfig()
        if target_resolution is not None:
            cfg.target_resolution = target_resolution
            cfg.target_resolution_arcmin = round(target_resolution * 60, 4)
        if resampling is not None:
            cfg.resampling = resampling  # type: ignore[assignment]
            if source == "viirs":
                cfg.variable_resampling["flood_fraction"] = resampling
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
            hits = sorted(candidate.glob(f"{event}_*_viirs_flood_fraction.tif"))
            if not hits:
                hits = sorted(candidate.glob(f"{event}_*_viirs_raw.tif"))
            if hits:
                processed_dir, tif_files = candidate, hits
                break
        # Fallback: check root directly (files may be flat)
        hits = sorted(root.glob(f"{event}_*_viirs_flood_fraction.tif"))
        if not hits:
            hits = sorted(root.glob(f"{event}_*_viirs_raw.tif"))
        if hits:
            processed_dir, tif_files = root, hits
            break

        # KuroSiwo deep pattern: <root>/<case>/viirs/processed/<case>_<date>_*.tif
        for vdir in sorted(root.rglob("viirs/processed/")):
            hits = sorted(vdir.glob(f"{event}_*_viirs_flood_fraction.tif"))
            if not hits:
                hits = sorted(vdir.glob(f"{event}_*_viirs_raw.tif"))
            if hits:
                processed_dir, tif_files = vdir, hits
                break
        if tif_files:
            break

    if not tif_files:
        fail(f"No processed VIIRS files found matching '{event}'")
        console.print("  Tried: cache dir, scripts/data/, and repository root.")
        console.print("  Run 'atlantis fetch' first, or use --input to point to existing data.")
        raise typer.Exit(code=1)

    output_path = output_root / source / "harmonised"
    output_path.mkdir(parents=True, exist_ok=True)

    resolution_str = f"{harmoniser.config.target_resolution_arcmin} arcmin"
    command_header("harmonise", subtitle=f"{event} · {source} · {resolution_str}")
    console.print(f"[bold]Harmonising {len(tif_files)} file(s)[/bold]")
    console.print(f"[bold]Input:[/bold] {processed_dir}")
    console.print(f"[bold]Output:[/bold] {output_path}")
    console.print(f"[bold]Target resolution:[/bold] {resolution_str} ({harmoniser.config.target_resolution:.8f}°)")
    console.print(f"[bold]Resampling:[/bold] {harmoniser.config.resampling}")

    if dry_run:
        dry_rows = []
        for tf in tif_files:
            stem = tf.stem.replace("flood_fraction", "harmonised").replace("raw", "harmonised")
            out = output_path / f"{stem}.tif"
            dry_rows.append([tf.name, out.name])
        console.print(summary_table("Dry run — would process", ["Input file", "Output file"], dry_rows))
        return

    harmonised_count = 0
    with make_progress() as progress:
        task = progress.add_task("[cyan]Harmonising[/cyan]", total=len(tif_files))
        for tif_path in tif_files:
            # Determine if this is flood_fraction, raw, or quality_mask
            input_var = "flood_fraction" if "flood_fraction" in tif_path.name else "raw"
            stem = tif_path.stem.replace("flood_fraction", "harmonised").replace("raw", "harmonised")
            out_path = output_path / f"{stem}.tif"

            progress.update(task, description=f"[cyan]Harmonising[/cyan] {tif_path.name}")
            ds = rxr.open_rasterio(tif_path).squeeze(drop=True).to_dataset(name=input_var)
            ds_harmonised = harmoniser.harmonise(ds, source_id=source, flood_variable=input_var)

            flood_var = input_var if input_var in ds_harmonised.data_vars else "flood_fraction"
            write_harmonised_raster(ds_harmonised[flood_var], out_path)
            harmonised_count += 1
            progress.advance(task)

    ok(f"Wrote {harmonised_count} harmonised file(s) to {output_path}")


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

    command_header("archive", subtitle=f"{event} · {source or 'all'}")
    console.print(f"[bold]Archiving event:[/bold] {event}")
    console.print(f"[bold]Archive root:[/bold] {archive_root}")

    if source:
        console.print(f"[bold]Source:[/bold] {source}")
    else:
        console.print("[bold]Source:[/bold] all available")

    if raw_only:
        info("Writing raw archive only")
    else:
        info("Writing raw + ML-ready archives")

    warn("Archive writing not yet implemented")


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

    command_header("validate")
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
        info("ML validation: enabled")

    warn("Validation not yet implemented")


@cli.command("list-sources")
def list_sources_cmd() -> None:
    """List all available data sources."""
    sources = list_fetchers()
    command_header("list-sources")

    source_descriptions = {
        "gfm": "Global Flood Monitor (STAC/EODC)",
        "viirs": "VIIRS Flood Detection (NOAA)",
        "rfm": "Regional Flood Model (Phase C)",
    }
    rows = [[src, source_descriptions.get(src, "No description")] for src in sources]
    console.print(summary_table("Available Data Sources", ["Name", "Description"], rows))


@cli.command()
def setup(
    check_only: bool = typer.Option(
        False,
        "--check-only",
        help="Only verify assets are present without modifying anything.",
    ),
    update_hashes: bool = typer.Option(
        False,
        "--update-hashes",
        help="Recompute SHA-256 hashes and write them to config/asset_hashes.json.",
    ),
) -> None:
    """Bootstrap required data assets (VIIRS AOI grid, KuroSiwo catalogue).

    Missing tracked files are automatically restored from git.  Run this
    once after cloning, or whenever a new data source is added.
    """
    from atlantis.utils.setup import run_setup

    command_header("setup")
    success = run_setup(auto_fix=not check_only, output=console, update_hashes=update_hashes)
    if not success:
        raise typer.Exit(code=1)


@cli.command()
def demo(
    output_dir: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output directory (default: data/Valencia_2024).",
    ),
    harmonise: bool = typer.Option(
        True,
        "--harmonise/--no-harmonise",
        help="Harmonise the peak-flood date to 1 arcmin. Default: on.",
    ),
    stream: bool = typer.Option(
        True,
        "--stream/--no-stream",
        help="Stream remote tiles. Default: on.",
    ),
) -> None:
    """Run the Valencia 2024 flood example.

    Fetches VIIRS data for the Valencia flood event (Oct–Nov 2024),
    plots the peak-flood date, and optionally harmonises to 1 arcmin.
    A quick way to verify that everything is working end-to-end.
    """
    from datetime import date

    from atlantis.utils.setup import get_missing_assets

    # Pre-flight: check assets are present
    missing = get_missing_assets()
    if missing:
        fail("Cannot run demo — required assets are missing:")
        for item in missing:
            console.print(f"  - {item}")
        console.print("\nRun [bold]uv run atlantis setup[/bold] first.")
        raise typer.Exit(code=1)

    out = output_dir or Path("data/Valencia_2024")
    out.mkdir(parents=True, exist_ok=True)

    command_header("demo", subtitle="Valencia 2024 flood")

    event = FloodEvent(
        event_id="Valencia_2024",
        bbox=(-1.5, 38.8, 0.5, 40.0),
        start_date=date(2024, 10, 29),
        end_date=date(2024, 11, 4),
    )

    fetcher_cls = get_fetcher("viirs")
    fetcher = fetcher_cls(
        classify=True,
        stream=stream,
        strategy="peak",
        keep_processed=True,
    )

    viirs_dir = out / "viirs"

    console.print(f"[bold]Event:[/bold] {event.event_id}")
    console.print(f"[bold]BBox:[/bold]  {event.bbox}")
    console.print(f"[bold]Dates:[/bold] {event.start_date} → {event.end_date}")
    console.print(f"[bold]Output:[/bold] {out}\n")

    with step_status("Fetching VIIRS tiles…"):
        fetch_results = fetcher.fetch(event, viirs_dir)
    if not fetch_results:
        warn("No VIIRS data found for this region/date range.")
        raise typer.Exit(code=1)

    _report_viirs_fetch_writes(fetch_results, keep_processed=True)

    # Select best date
    best_result, best_date_label = _select_best_result(fetcher, fetch_results)
    best_ds = fetcher.to_dataset(best_result)

    # Plot
    plot_dir_path = viirs_dir / "plots"
    plot_dir_path.mkdir(parents=True, exist_ok=True)
    png_path = plot_dir_path / f"Valencia_2024_{best_date_label}_viirs.png"
    with step_status("Plotting peak-flood date…"):
        _plot_viirs(best_ds, "Valencia_2024", best_date_label, output_png_path=png_path)

    # Harmonise
    if harmonise:
        harm_dir = viirs_dir / "harmonised"
        with step_status("Harmonising to 1 arcmin…"):
            _harmonise_viirs(
                best_ds,
                "Valencia_2024",
                best_date_label,
                harm_dir=harm_dir,
                plot_dir=plot_dir_path,
            )

    console.print("")
    ok("Demo complete!")
    from atlantis.utils.ui import file_tree as _ft

    if harmonise:
        output_files = [
            png_path,
            harm_dir / f"Valencia_2024_{best_date_label}_viirs_harmonised.tif",
            plot_dir_path / f"Valencia_2024_{best_date_label}_viirs_harmonised.png",
        ]
    else:
        output_files = [png_path]
    console.print(_ft(str(out), output_files))


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

    command_header("list-events")
    console.print(f"[bold]Archive:[/bold] {archive_root}")
    warn("No events found (archive not yet implemented)")


if __name__ == "__main__":
    cli()
