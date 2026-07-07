"""CLI entrypoints for Atlantis."""

import sys
from datetime import date
from pathlib import Path

import requests
import typer
from dotenv import load_dotenv
from loguru import logger
from rasterio.enums import Resampling

# Load credentials from .env at the repo root so fetchers that consult
# os.environ directly (e.g. MODIS EARTHDATA_TOKEN) see them. Existing
# environment variables take precedence (override=False).
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=False)

from atlantis.config import HarmoniseConfig, get_config  # noqa: E402

# Import fetchers to register them
from atlantis.fetchers import fetcher_registry, get_fetcher, gfm, list_fetchers, modis, rfm, viirs  # noqa: E402, F401
from atlantis.fetchers.base import FetchResult  # noqa: E402
from atlantis.harmoniser import write_harmonised_raster  # noqa: E402
from atlantis.models.event import FloodEvent  # noqa: E402
from atlantis.utils.checklist import is_task_checklist_active, task_checklist  # noqa: E402
from atlantis.utils.kurosiwo import (  # noqa: E402
    KUROSIWO_DEFAULT_CATALOGUE,
    KUROSIWO_DEFAULT_METADATA,
    build_kurosiwo_flood_events,
    build_kurosiwo_flood_events_from_catalogue,
    write_kurosiwo_metadata_csv,
)
from atlantis.utils.plot import (  # noqa: E402
    GFM_ENSEMBLE_FLOOD_EXTENT_CODES,
    GFM_REFERENCE_WATER_MASK_CODES,
    MODIS_RAW_CODES,
    VIIRS_RAW_CODES,
    date_from_filename,
    pixel_stats_classified,
    pixel_stats_raw,
    plot_classified,
    plot_raw,
)
from atlantis.utils.ui import (  # noqa: E402
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

cli = typer.Typer(help="Atlantis — ML-ready flood inundation archive pipeline.")

_VERBOSE_OUTPUT = False


def _should_emit_verbose_log(record: dict) -> bool:
    """Keep Atlantis logs out of stderr while a live checklist is active."""
    logger_name = record["name"]
    if logger_name.startswith("atlantis") and is_task_checklist_active():
        return False
    return True


def _fetch_animation_profile(source_id: str) -> str | None:
    """Return the fixed animation profile to use for a fetch step, if any."""
    if source_id in {"viirs", "modis"}:
        return f"{source_id}_fetch"
    return None


def _fetch_step_names(source_id: str) -> list[str]:
    """Return the top-level fetch-phase checklist rows for *source_id*."""
    if source_id in {"viirs", "modis"}:
        return ["Fetch tiles", "Process tiles"]
    return ["Fetch tiles"]


@cli.callback()
def _main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose debug logging."),
) -> None:
    """Atlantis CLI — configure global options."""
    global _VERBOSE_OUTPUT

    _VERBOSE_OUTPUT = verbose
    logger.remove()
    logger.disable("atlantis")
    if verbose:
        logger.enable("atlantis")
        logger.add(
            sys.stderr,
            level="DEBUG",
            format="<dim>{time:HH:mm:ss}</dim> | <level>{message}</level>",
            colorize=True,
            filter=_should_emit_verbose_log,
        )


# ── Shared plot + harmonise helper ──────────────────────────────────────────


SOURCE_PRETTY_NAMES: dict[str, str] = {
    "viirs": "VIIRS",
    "modis": "MODIS",
    "gfm": "GFM",
    "rfm": "RFM",
}

SOURCE_RESOLUTION_LABELS: dict[str, str] = {
    "viirs": "375 m",
    "modis": "250 m",
    "gfm": "~80 m",
}


def _pretty_source(source_id: str) -> str:
    return SOURCE_PRETTY_NAMES.get(source_id, source_id.upper())


def _classified_layer_label(source_id: str) -> str:
    return "flood fraction" if source_id == "viirs" else "flood extent"


def _resolution_label(source_id: str) -> str:
    return SOURCE_RESOLUTION_LABELS.get(source_id, "native")


def _ds_is_classified(ds, source_id: str | None = None) -> bool:
    """True when a fetched dataset holds derived layers for its source.

    Derived-layer names can overlap with native bands of other sources
    (e.g. ``exclusion_mask`` is native for GFM but derived for VIIRS/MODIS),
    so the check is scoped to the dataset's source when known.
    """
    if source_id is None:
        source_id = ds.attrs.get("source_id")
    if source_id is None:
        # Last-resort fallback: any derived layer from any source.
        from atlantis.layers import all_registries

        derived_names = {layer.name for registry in all_registries().values() for layer in registry.list_derived()}
        return any(name in ds.data_vars for name in derived_names)

    from atlantis.layers import all_registries

    registry = all_registries().get(source_id)
    if registry is None:
        return False
    derived_names = {layer.name for layer in registry.list_derived()}
    return any(name in ds.data_vars for name in derived_names)


def _date_label(result: FetchResult) -> str:
    """Human-readable date label for a fetch result."""
    if result.date_token:
        return _date_label_from_token(result.date_token)
    if result.files:
        return date_from_filename(result.files[0].name)
    return "unknown"


def _date_label_from_token(token: str) -> str:
    """Format an 8-digit date token as YYYY-MM-DD, or return as-is."""
    if len(token) == 8 and token.isdigit():
        return f"{token[:4]}-{token[4:6]}-{token[6:8]}"
    return token


# Backwards-compatibility alias used by tests / external tooling.
_viirs_date_label = _date_label


def _report_fetch_writes(
    source_id: str, fetch_results: list[FetchResult], *, keep_processed: bool, strategy: str = "peak"
) -> None:
    """Print what a fetcher persisted (disk vs in-memory composite/peak date)."""
    disk_files = sum(len(result.files) for result in fetch_results)
    if disk_files:
        ok(f"Wrote {disk_files} files")
        all_paths = [path for result in fetch_results for path in result.files]
        console.print(file_tree(source_id, all_paths))
        return
    if fetch_results and not keep_processed:
        label = _date_label(fetch_results[0])
        if label == "aggregated":
            info("Aggregated composite: processed in memory (no processed/ GeoTIFFs)")
        elif strategy == "all" and len(fetch_results) > 1:
            info(f"{len(fetch_results)} date(s) processed in memory (no processed/ GeoTIFFs)")
        else:
            info(f"Peak-flood date {label}: processed in memory (no processed/ GeoTIFFs)")


# Backwards-compatibility wrapper retained for VIIRS callers / tests.
def _report_viirs_fetch_writes(fetch_results: list[FetchResult], *, keep_processed: bool) -> None:
    _report_fetch_writes("viirs", fetch_results, keep_processed=keep_processed)


def _report_empty_fetch(source_id: str, fetcher) -> None:
    """Explain why a fetcher returned no results, using diagnostics when available.

    Generic fetchers fall back to the previous one-line message; VIIRS, MODIS
    and GFM expose structured diagnostics via ``fetcher.last_diagnostics`` and we
    translate them into actionable guidance here.
    """
    diagnostics = getattr(fetcher, "last_diagnostics", None)
    if diagnostics is None:
        warn("No files were fetched")
        return

    if source_id == "modis":
        _report_empty_modis_fetch(diagnostics)
        return

    if source_id == "gfm":
        _report_empty_gfm_fetch(diagnostics)
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

    if diagnostics.network_unreachable:
        warn(
            f"Reason: backend '{diagnostics.backend}' is unreachable "
            f"({diagnostics.network_failures}/{diagnostics.dates_probed} listing request(s) failed)."
        )
        if diagnostics.last_network_error:
            info(f"Last network error: {diagnostics.last_network_error}")
        if diagnostics.backend == "gmu_legacy":
            info(
                "Hint: jpssflood.gmu.edu is intermittently offline. Retry later "
                "(ideally from a non-cloud network), or use --viirs-backend noaa_s3 "
                "for years 2012–2020 / 2023–2026."
            )
        else:
            info("Hint: check your network connection or retry shortly.")
        return

    if diagnostics.listings_all_empty:
        warn(
            f"Reason: backend '{diagnostics.backend}' returned no listings for any of "
            f"the {diagnostics.dates_probed} requested date(s)."
        )
        if diagnostics.backend == "gmu_legacy":
            info(
                "Hint: the GMU legacy host can be intermittently offline. Retry with "
                "--no-stream and/or from a non-cloud network."
            )
        else:
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
        if diagnostics.backend == "gmu_legacy":
            info("Hint: try widening the date window by 1–2 days for legacy coverage gaps.")
        return

    warn(
        f"Reason: backend '{diagnostics.backend}' produced no processable tiles "
        f"({diagnostics.result_count} search results across {diagnostics.dates_probed} date(s))."
    )


def _report_empty_modis_fetch(diagnostics) -> None:
    """Translate ``ModisSearchDiagnostics`` into actionable CLI guidance."""
    warn("No files were fetched.")

    if getattr(diagnostics, "auth_token_missing", False):
        warn("Reason: EARTHDATA_TOKEN is not set in the environment.")
        info("Hint: register at https://urs.earthdata.nasa.gov/ and run\n      export EARTHDATA_TOKEN='YOUR_TOKEN'")
        return

    if not getattr(diagnostics, "tile_count", 1):
        warn("Reason: event bbox maps to zero MODIS tiles (likely dateline-crossing).")
        info("Hint: split the bbox at ±180° or widen / verify the coordinates.")
        return

    if getattr(diagnostics, "outside_lance_window", False):
        warn("Reason: requested date(s) fall outside the LANCE NRT retention window (~last week).")
        info(
            "Hint: switch to --modis-backend laads_hdf4 for historical dates "
            "(2003–2025 reprocessed, 2026+ archived NRT)."
        )
        return

    if diagnostics.year_coverage_gap:
        published = (
            ", ".join(str(y) for y in sorted(diagnostics.available_years)) if diagnostics.available_years else "unknown"
        )
        requested = ", ".join(str(y) for y in sorted(diagnostics.requested_years))
        warn(f"Reason: backend '{diagnostics.backend}' does not publish data for the requested year(s) ({requested}).")
        info(f"Published years on this backend: {published}")
        return

    if diagnostics.network_unreachable:
        warn(
            f"Reason: backend '{diagnostics.backend}' is unreachable "
            f"({diagnostics.network_failures}/{diagnostics.dates_probed} listing request(s) failed)."
        )
        if diagnostics.last_network_error:
            info(f"Last network error: {diagnostics.last_network_error}")
        if diagnostics.backend == "lance_geotiff":
            info(
                "Hint: nrt3 is the primary; the fetcher already tried nrt4. Verify your "
                "network or fall back to --modis-backend laads_hdf4."
            )
        else:
            info("Hint: check your network connection or retry shortly.")
        return

    if diagnostics.listings_all_empty:
        warn(
            f"Reason: backend '{diagnostics.backend}' returned no listings for any of "
            f"the {diagnostics.dates_probed} requested date(s)."
        )
        if diagnostics.backend == "lance_geotiff":
            info("Hint: this date may have rolled out of the LANCE retention window. Try --modis-backend laads_hdf4.")
        else:
            info(
                "Hint: try widening --start-date/--end-date or verify EARTHDATA_TOKEN "
                "is valid (token expiry surfaces here as empty listings)."
            )
        return

    if diagnostics.no_tile_match_in_listings:
        warn(
            f"Reason: {diagnostics.dates_with_listings} date(s) had listings, but none "
            f"contained tiles for the {diagnostics.tile_count} intersecting tile(s)."
        )
        info(
            "Hint: the (h, v) tiles intersecting this bbox were not produced for the "
            "requested dates. Widen the date window by 1–2 days or try a different composite."
        )
        return

    warn(
        f"Reason: backend '{diagnostics.backend}' produced no processable tiles "
        f"({diagnostics.result_count} search results across {diagnostics.dates_probed} date(s))."
    )


def _report_empty_gfm_fetch(diagnostics) -> None:
    """Translate ``GfmSearchDiagnostics`` into actionable CLI guidance."""
    warn("No files were fetched.")

    if diagnostics.network_unreachable:
        warn(f"Reason: GFM STAC endpoint '{diagnostics.api_url}' is unreachable.")
        if diagnostics.last_network_error:
            info(f"Last network error: {diagnostics.last_network_error}")
        info("Hint: check your network connection or retry shortly. The EODC STAC API is at https://stac.eodc.eu/")
        return

    if diagnostics.no_items_found:
        warn("Reason: the STAC search returned no items for this bbox and date range.")
        info(
            "Hint: GFM coverage depends on Sentinel-1 acquisition scheduling. "
            "Try widening --start-date/--end-date by a few days, or verify the bbox is correct."
        )
        return

    warn(
        f"Reason: STAC search returned {diagnostics.items_found} item(s) across "
        f"{diagnostics.dates_found} date(s) but none produced processable output."
    )


def _select_best_result(
    fetcher,
    fetch_results,
):
    """Select the fetch result with the highest flood pixel count."""
    if len(fetch_results) == 1:
        return fetch_results[0], _date_label(fetch_results[0])

    best_result = None
    best_date_label = ""
    best_flood_count = 0

    for result in fetch_results:
        ds = fetcher.to_dataset(result)
        date_label = _date_label(result)
        if _ds_is_classified(ds, source_id=fetcher.source_id):
            flooded = pixel_stats_classified(ds["flood_fraction"].values, name=date_label)
            if flooded > best_flood_count:
                best_flood_count = flooded
                best_result = result
                best_date_label = date_label
        else:
            pixel_stats_raw(ds["raw"].values, name=date_label)
            raw_vals = ds["raw"].values.ravel()
            flooded = int(((raw_vals >= 101) & (raw_vals <= 200)).sum())
            if flooded > best_flood_count:
                best_flood_count = flooded
                best_result = result
                best_date_label = date_label

    if best_result is None:
        best_result = fetch_results[0]
        best_date_label = _date_label(fetch_results[0])

    return best_result, best_date_label


def _plot_source(
    best_ds,
    event_id,
    date_label,
    *,
    source_id: str,
    output_png_path,
    announce: bool = True,
):
    """Save a PNG visualisation of the peak-flood date for any source."""
    pretty = _pretty_source(source_id)
    res = _resolution_label(source_id)
    if _ds_is_classified(best_ds, source_id=source_id):
        layer_label = _classified_layer_label(source_id)
        plot_classified(
            best_ds["flood_fraction"],
            title=f"{event_id}: {pretty} {layer_label} {date_label} ({res})",
            output_path=output_png_path,
            announce=announce,
        )
    elif "ensemble_flood_extent" in best_ds:
        # GFM native / raw mode — plot each native band with its own legend.
        plot_raw(
            best_ds["ensemble_flood_extent"],
            title=f"{event_id}: {pretty} ensemble_flood_extent {date_label} ({res})",
            output_path=output_png_path,
            codes=GFM_ENSEMBLE_FLOOD_EXTENT_CODES,
            legend_title="GFM ensemble_flood_extent codes",
            announce=announce,
        )
        if "reference_water_mask" in best_ds:
            mask_png = output_png_path.parent / output_png_path.name.replace(".png", "_reference_water_mask.png")
            plot_raw(
                best_ds["reference_water_mask"],
                title=f"{event_id}: {pretty} reference_water_mask {date_label} ({res})",
                output_path=mask_png,
                codes=GFM_REFERENCE_WATER_MASK_CODES,
                legend_title="GFM reference_water_mask codes",
                announce=announce,
            )
    else:
        # Raw composite: pick the legend matching the source's pixel codes.
        codes = MODIS_RAW_CODES if source_id == "modis" else VIIRS_RAW_CODES
        legend_title = "MODIS MCDWD codes" if source_id == "modis" else "VIIRS pixel codes"
        raw = best_ds["raw"]
        if source_id != "modis":
            # Collapse the continuous flood range (101–200) onto the single
            # representative code 100 so the categorical legend matches the render.
            raw = raw.where((raw < 101) | (raw > 200), 100)
        plot_raw(
            raw,
            title=f"{event_id}: {pretty} raw composite {date_label} ({res})",
            output_path=output_png_path,
            codes=codes,
            legend_title=legend_title,
            announce=announce,
        )


# Backwards-compatibility wrapper used by external callers / tests.
def _plot_viirs(
    best_ds,
    event_id,
    date_label,
    *,
    output_png_path,
    announce: bool = True,
):
    """Save a PNG visualisation of the VIIRS peak-flood date."""
    _plot_source(
        best_ds,
        event_id,
        date_label,
        source_id="viirs",
        output_png_path=output_png_path,
        announce=announce,
    )


def _harmonise_safely(fn, label: str, *args, **kwargs) -> bool:
    """Run a harmonise call, warning and continuing instead of aborting on error.

    Returns True on success, False if the harmonise raised (a warning is logged).
    """
    try:
        fn(*args, **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001 — harmonise failures must not abort the run
        warn(f"Harmonise failed for {label}: {exc}")
        return False


def _raw_nodata_for_source(source_id: str) -> int | None:
    """Return the raw-code nodata sentinel used by a source's Atlantis outputs."""
    if source_id == "viirs":
        # Atlantis writes raw VIIRS rasters with nodata=0 for clip / mosaic fill.
        return 0
    if source_id == "modis":
        return 255
    return None


def _prepare_raw_dataset_for_harmonise(best_ds, *, source_id: str):
    """Attach source-appropriate raw nodata metadata before NN reprojection."""
    if "raw" not in best_ds:
        return best_ds

    preferred_nodata = _raw_nodata_for_source(source_id)
    if preferred_nodata is None:
        return best_ds

    # Deep-copy only the raw band we mutate; the surrounding dataset is shallow
    # copied so the other (untouched) variables are shared rather than cloned.
    raw = best_ds["raw"].copy(deep=True)
    try:
        current_nodata = raw.rio.nodata
    except Exception:
        current_nodata = None

    if current_nodata is None:
        raw.rio.write_nodata(preferred_nodata, inplace=True)
        raw.attrs.setdefault("nodata", preferred_nodata)
        raw.attrs.setdefault("_FillValue", preferred_nodata)

    prepared = best_ds.copy(deep=False)
    prepared["raw"] = raw
    return prepared


def _harmonise_source(
    best_ds,
    event_id,
    date_label,
    *,
    source_id: str,
    harm_dir,
    plot_dir,
    announce: bool = True,
):
    """Reproject + normalise and save harmonised GeoTIFF + PNG (any source).

    Handles both classified datasets (``flood_fraction``) and native / raw
    datasets (a single ``raw`` code band). In native mode the raw codes are
    nearest-neighbour reprojected to the 1-arcmin grid and written as-is
    (uint8 codes); no fraction derivation is performed.

    Returns the xarray Dataset produced by the harmoniser for downstream use.
    """
    from atlantis.harmoniser import Harmoniser

    pretty = _pretty_source(source_id)
    harm_dir.mkdir(parents=True, exist_ok=True)
    plot_dir.mkdir(parents=True, exist_ok=True)
    h = Harmoniser()

    tif_path = harm_dir / f"{event_id}_{date_label}_{source_id}_harmonised.tif"
    png_harm_path = plot_dir / f"{event_id}_{date_label}_{source_id}_harmonised.png"

    if _ds_is_classified(best_ds, source_id=source_id):
        ds_harm = h.harmonise(best_ds, source_id=source_id)
        write_harmonised_raster(ds_harm["flood_fraction"], tif_path)
        if announce:
            console.print(f"  Harmonised → {tif_path.name}")
        layer_label = _classified_layer_label(source_id)
        plot_classified(
            ds_harm["flood_fraction"],
            title=f"{event_id}: {pretty} harmonised {layer_label} {date_label} (1 arcmin)",
            output_path=png_harm_path,
            announce=announce,
        )
        return ds_harm

    # Native / raw mode — NN-reproject the raw codes to the 1-arcmin grid and
    # write them as-is (mirrors the GFM native harmonise path).
    ds_harm = h.reprojector.reproject(_prepare_raw_dataset_for_harmonise(best_ds, source_id=source_id))
    write_harmonised_raster(ds_harm["raw"], tif_path)
    if announce:
        console.print(f"  Harmonised → {tif_path.name}")
    codes = MODIS_RAW_CODES if source_id == "modis" else VIIRS_RAW_CODES
    legend_title = "MODIS MCDWD codes" if source_id == "modis" else "VIIRS pixel codes"
    raw = ds_harm["raw"]
    if source_id != "modis":
        # Collapse the continuous flood range (101–200) onto the single
        # representative code 100 so the categorical legend matches the render.
        raw = raw.where((raw < 101) | (raw > 200), 100)
    plot_raw(
        raw,
        title=f"{event_id}: {pretty} harmonised raw composite {date_label} (1 arcmin)",
        output_path=png_harm_path,
        codes=codes,
        legend_title=legend_title,
        announce=announce,
    )
    return ds_harm


# Backwards-compatibility wrapper used by external callers / tests.
def _harmonise_viirs(
    best_ds,
    event_id,
    date_label,
    *,
    harm_dir,
    plot_dir,
    announce: bool = True,
):
    """Reproject + normalise and save harmonised GeoTIFF (in ``harm_dir``) + PNG (in ``plot_dir``).

    Returns the xarray Dataset produced by the harmoniser for downstream use.
    """
    return _harmonise_source(
        best_ds,
        event_id,
        date_label,
        source_id="viirs",
        harm_dir=harm_dir,
        plot_dir=plot_dir,
        announce=announce,
    )


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
    plot_dir,
):
    """Harmonise GFM data and save GeoTIFF (harm_dir) + optional PNG (plot_dir).

    Works for both classified mode (flood_fraction variable) and native / raw
    mode (ensemble_flood_extent + reference_water_mask variables).  In native
    mode the bands are NN-reprojected to 1-arcmin and written as-is (uint8
    codes); no fraction derivation is performed.
    """
    from atlantis.harmoniser import Harmoniser

    harm_dir.mkdir(parents=True, exist_ok=True)
    plot_dir.mkdir(parents=True, exist_ok=True)
    h = Harmoniser()

    if _ds_is_classified(ds, source_id="gfm"):
        # Classified mode
        ds_harm = h.harmonise(ds, source_id="gfm", flood_variable="flood_fraction")
        tif_path = harm_dir / f"{event_id}_{date_label}_gfm_harmonised.tif"
        write_harmonised_raster(ds_harm["flood_fraction"], tif_path)
        console.print(f"  Harmonised → {tif_path.name}")
        png_harm_path = plot_dir / f"{event_id}_{date_label}_gfm_harmonised.png"
        plot_classified(
            ds_harm["flood_fraction"],
            title=f"{event_id}: GFM harmonised flood extent {date_label} (1 arcmin)",
            output_path=png_harm_path,
        )
        return ds_harm

    # Native mode — reproject each band with NN, write as uint8 codes + plot bands
    ds_harm = h.reprojector.reproject(ds)
    band_legends = {
        "ensemble_flood_extent": (GFM_ENSEMBLE_FLOOD_EXTENT_CODES, "GFM ensemble_flood_extent codes"),
        "reference_water_mask": (GFM_REFERENCE_WATER_MASK_CODES, "GFM reference_water_mask codes"),
    }
    for var in ("ensemble_flood_extent", "reference_water_mask"):
        if var not in ds_harm:
            continue
        tif_path = harm_dir / f"{event_id}_{date_label}_gfm_{var}_harmonised.tif"
        write_harmonised_raster(ds_harm[var], tif_path)
        console.print(f"  Harmonised → {tif_path.name}")
        codes, legend_title = band_legends[var]
        png_harm_path = plot_dir / f"{event_id}_{date_label}_gfm_{var}_harmonised.png"
        plot_raw(
            ds_harm[var],
            title=f"{event_id}: GFM harmonised {var} {date_label} (1 arcmin)",
            output_path=png_harm_path,
            codes=codes,
            legend_title=legend_title,
        )

    return ds_harm


def _resolve_output_items(fetcher, fetch_results, strategy: str, src: str):
    """Resolve the ``(date_label, dataset)`` items to emit for a strategy.

    * ``all`` — one item per surviving date (full time-series).
    * ``peak`` — the single peak-flood date.
    * ``aggregate`` — the single combined result (its ``date_token`` is the
      aggregated range; the fallback is only used if that token is empty).
    """
    if strategy == "all":
        return [(_date_label(result), fetcher.to_dataset(result)) for result in fetch_results]
    if strategy == "peak":
        best_result, best_date_label = _select_best_result(fetcher, fetch_results)
        return [(best_date_label, fetcher.to_dataset(best_result))]
    # aggregate
    result = fetch_results[0]
    fallback = "gfm" if src == "gfm" else "aggregated"
    return [(result.date_token or fallback, fetcher.to_dataset(result))]


def _emit_source_outputs(
    *,
    src: str,
    fetcher,
    fetch_results,
    strategy: str,
    event: str,
    output_dir: Path,
    plot_dir,
    plot: bool,
    harmonise: bool,
    checklist,
) -> None:
    """Plot and/or harmonise the selected datasets for one source.

    Unifies the output stage across VIIRS / MODIS / GFM: it resolves the
    ``(label, dataset)`` items implied by *strategy* and runs the plot and
    harmonise steps uniformly. GFM uses its dedicated harmoniser (two native
    code bands); VIIRS / MODIS use the shared ``_harmonise_source``.
    """
    if not (plot or harmonise):
        return

    png_dir = plot_dir or (output_dir / src / "plots")
    harm_dir = output_dir / src / "harmonised"
    items = _resolve_output_items(fetcher, fetch_results, strategy, src)
    detail = f"{len(items)} date(s)" if strategy == "all" else items[0][0]

    if plot:
        with checklist.step("Plot outputs"):
            for date_label, ds in items:
                png_out = png_dir / f"{event}_{date_label}_{src}.png"
                _plot_source(ds, event, date_label, source_id=src, output_png_path=png_out)
        checklist.complete("Plot outputs", detail=detail)

    if harmonise:
        with checklist.step("Harmonise outputs"):
            for date_label, ds in items:
                if src == "gfm":
                    _harmonise_safely(
                        _harmonise_gfm,
                        date_label,
                        ds,
                        event,
                        date_label,
                        harm_dir=harm_dir,
                        plot_dir=png_dir,
                    )
                else:
                    _harmonise_safely(
                        _harmonise_source,
                        date_label,
                        ds,
                        event,
                        date_label,
                        source_id=src,
                        harm_dir=harm_dir,
                        plot_dir=png_dir,
                    )
        checklist.complete("Harmonise outputs", detail=detail)


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
    source: str | None = typer.Option(None, "--source", "-s", help="Data source (gfm, viirs, modis, rfm, all)"),
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
    modis_backend: str = typer.Option(
        "lance_geotiff",
        "--modis-backend",
        help="MODIS backend: lance_geotiff (streamable, ~1-week NRT) or laads_hdf4 (download, 2003+).",
    ),
    modis_composite: str = typer.Option(
        "F2",
        "--modis-composite",
        help="MODIS composite: F1, F1C, F2, F3. Default: F2 (2-day max-water composite).",
    ),
    classify: bool = typer.Option(
        True,
        "--classify/--no-classify",
        help="Emit the source's derived layers from the registry-backed layer catalogue. Default: on."
        " Use --no-classify to emit the native source layers untouched (raw codes/bands)."
        " See `atlantis list-layers` for the exact inventory.",
    ),
    stream: bool = typer.Option(
        True,
        "--stream/--no-stream",
        help="Stream remote tiles via GDAL /vsicurl/ without downloading to disk"
        " (saves storage, requires network during processing). Default: on."
        " Use --no-stream to download tiles to disk instead."
        " MODIS: only valid with --modis-backend lance_geotiff.",
    ),
    plot: bool = typer.Option(
        False,
        "--plot",
        help="Save a PNG visualisation of the peak-flood date (VIIRS / MODIS / GFM).",
    ),
    plot_dir: Path | None = typer.Option(
        None,
        "--plot-dir",
        help="Directory to write PNG files (default: <output>/plots/).",
    ),
    harmonise: bool = typer.Option(
        False,
        "--harmonise",
        help="Harmonise the peak-flood date to 1 arcmin after fetching (VIIRS / MODIS / GFM).",
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
    peak_days_before: int = typer.Option(
        0,
        "--peak-days-before",
        help="(≥ 0) Filter dates to this many days BEFORE the computed peak. 0 = no filtering.",
    ),
    peak_days_after: int = typer.Option(
        0,
        "--peak-days-after",
        help="(≥ 0) Filter dates to this many days AFTER the computed peak. 0 = no filtering.",
    ),
    peak_window_days: int = typer.Option(
        0,
        "--peak-window-days",
        help="Symmetric shorthand: set both --peak-days-before and --peak-days-after to this value."
        " Cannot be combined with --peak-days-before / --peak-days-after.",
    ),
    max_observations: int = typer.Option(
        0,
        "--max-observations",
        help="Maximum number of dates to return after windowing. 0 = no limit (VIIRS / MODIS / GFM).",
    ),
    peak_priority: str = typer.Option(
        "post",
        "--peak-priority",
        help="Subsampling bias when --max-observations is set: post (post-event first),"
        " pre (pre-event first), or balanced (alternating ±1, ±2, …). VIIRS / MODIS / GFM.",
    ),
    gfm_coarsen_factor: int = typer.Option(
        4,
        "--gfm-coarsen-factor",
        help="GFM spatial coarsening factor before reprojection (default 4).",
    ),
    gfm_resampling: str = typer.Option(
        "average",
        "--gfm-resampling",
        help="GFM resampling method for reprojection to EPSG:4326.",
    ),
) -> None:
    """Fetch raw inundation data from specified source(s).

    Args:
        event: Flood event ID to fetch data for.
        source: Data source to fetch from. Options: gfm, viirs, modis, rfm, all.
        output_dir: Directory to save downloaded files.
        bbox: Bounding box as west south east north for direct event construction.
        start_date: Start date for direct event construction in YYYY-MM-DD format.
        end_date: End date for direct event construction in YYYY-MM-DD format.
        viirs_backend: Which VIIRS backend to use (noaa_s3 or gmu_legacy).
        viirs_format: Which VIIRS data format to fetch (tif, netcdf, shapezip, png). Only tif is implemented.
        modis_backend: Which MODIS backend to use (lance_geotiff or laads_hdf4).
        modis_composite: Which MCDWD composite to fetch (F1, F1C, F2, F3).
        classify: If True, write water/flood-fraction plus reference/exclusion layers instead of raw data.
            MODIS adds a recurring_flood layer when classified.
        stream: If True, stream remote tiles without downloading to disk. For MODIS, only
            valid with --modis-backend lance_geotiff.
        plot: Save PNG visualisation of the peak-flood date (VIIRS / MODIS / GFM).
        plot_dir: Directory for PNG output (default: <output>/plots/).
        harmonise: Harmonise the peak-flood date to 1 arcmin (VIIRS / MODIS / GFM).
        strategy: How to handle multiple dates: peak, aggregate, all.
        keep_processed: Write intermediate processed/ GeoTIFFs.
        peak_days_before: Days before the peak to include (window filter, VIIRS / MODIS / GFM).
        peak_days_after: Days after the peak to include (window filter, VIIRS / MODIS / GFM).
        peak_window_days: Symmetric shorthand for peak_days_before == peak_days_after.
        max_observations: Maximum number of dates to return after windowing (VIIRS / MODIS / GFM).
        peak_priority: Subsampling bias when max_observations is set (VIIRS / MODIS / GFM).
        gfm_coarsen_factor: GFM spatial coarsening factor before reprojection.
        gfm_resampling: GFM resampling method for reprojection to EPSG:4326.
    """
    config = get_config()
    output_dir = output_dir or config.fetcher.cache_dir / "raw" / event
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        gfm_resampling_enum = Resampling[gfm_resampling]
    except KeyError as exc:
        valid = [r.name for r in Resampling]
        raise typer.BadParameter(
            f"--gfm-resampling '{gfm_resampling}' is not valid. Choose from: {', '.join(valid)}"
        ) from exc

    if source is None or source == "all":
        sources = list_fetchers()
    else:
        sources = [source]

    command_header("fetch", subtitle=f"{event} · sources={', '.join(sources)}")
    console.print(f"[bold]Output:[/bold] {output_dir}")
    if "viirs" in sources:
        mode_label = "stream" if stream else "download"
        console.print(f"[bold]VIIRS backend:[/bold] {viirs_backend} ({mode_label}, format={viirs_format})")
        if viirs_backend == "gmu_legacy":
            info("Legacy backend note: year coverage is inferred by probing each requested date.")
    if "modis" in sources:
        modis_mode = "stream" if stream and modis_backend == "lance_geotiff" else "download"
        console.print(f"[bold]MODIS backend:[/bold] {modis_backend} ({modis_mode}, composite={modis_composite})")
        if modis_backend == "laads_hdf4":
            info("LAADS HDF4 backend: tiles are downloaded; --stream is ignored.")
    if "gfm" in sources:
        if not stream:
            info("GFM always streams via STAC/COG; --no-stream is ignored.")
        if classify:
            if not harmonise:
                info(
                    "GFM (classified): processed flood_fraction is uint8 % (~80 m);"
                    " --harmonise reprojects it onto the canonical 1-arcmin grid."
                )
        else:
            info(
                "GFM native mode: outputs ensemble_flood_extent and reference_water_mask"
                " as-is (~80 m); --harmonise downsamples to 1-arcmin (nearest-neighbour)."
            )

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
        except KeyError:
            fail(f"Unknown source '{src}'")
            continue
        section_rule(src)
        fetcher_kwargs = {}
        # Peak-window flags (shared by VIIRS, MODIS, GFM) get resolved here
        # so the report code downstream can reference them unconditionally.
        effective_days_before = 0
        effective_days_after = 0
        if src in ("viirs", "modis", "gfm"):
            if peak_window_days > 0 and (peak_days_before > 0 or peak_days_after > 0):
                raise typer.BadParameter(
                    "--peak-window-days cannot be combined with --peak-days-before / --peak-days-after"
                )
            effective_days_before = peak_window_days if peak_window_days > 0 else peak_days_before
            effective_days_after = peak_window_days if peak_window_days > 0 else peak_days_after

        if src == "viirs":
            fetcher_kwargs = {
                "backend": viirs_backend,
                "data_format": viirs_format,
                "classify": classify,
                "stream": stream,
                "strategy": strategy,
                "keep_processed": keep_processed,
                "peak_days_before": effective_days_before,
                "peak_days_after": effective_days_after,
                "max_observations": max_observations,
                "peak_priority": peak_priority,
            }
        elif src == "modis":
            effective_stream = stream and modis_backend == "lance_geotiff"
            fetcher_kwargs = {
                "backend": modis_backend,
                "composite": modis_composite,
                "classify": classify,
                "stream": effective_stream,
                "strategy": strategy,
                "keep_processed": keep_processed,
                "peak_days_before": effective_days_before,
                "peak_days_after": effective_days_after,
                "max_observations": max_observations,
                "peak_priority": peak_priority,
            }
        elif src == "gfm":
            fetcher_kwargs = {
                "coarsen_factor": gfm_coarsen_factor,
                "resampling": gfm_resampling_enum,
                "classify": classify,
                "strategy": strategy,
                "keep_processed": keep_processed,
                "peak_days_before": effective_days_before,
                "peak_days_after": effective_days_after,
                "max_observations": max_observations,
                "peak_priority": peak_priority,
            }
        fetcher = fetcher_cls(**fetcher_kwargs)
        if flood_event is None:
            warn("Event catalogue lookup not yet implemented; provide --bbox/--start-date/--end-date")
            continue

        step_names = _fetch_step_names(src)
        if plot:
            step_names.append("Plot outputs")
        if harmonise:
            step_names.append("Harmonise outputs")

        with task_checklist(step_names, verbose=_VERBOSE_OUTPUT) as checklist:
            try:
                profile = _fetch_animation_profile(src)
                fetch_step_name = "Process tiles" if profile is not None else "Fetch tiles"
                with checklist.step(fetch_step_name, profile=profile, pre_step="Fetch tiles" if profile else None):
                    fetch_results = fetcher.fetch(flood_event, output_dir / src)
            except requests.RequestException as exc:
                fail(f"Network error while fetching {src}: {exc}")
                if src == "viirs" and viirs_backend == "gmu_legacy":
                    info(
                        "Hint: jpssflood.gmu.edu is intermittently offline. Retry later, or "
                        "use --viirs-backend noaa_s3 for years 2012–2020 / 2023–2026."
                    )
                continue

            if not fetch_results:
                checklist.warn("Fetch tiles", detail="no files")
                _report_empty_fetch(src, fetcher)
                continue

            written_files = sum(len(result.files) for result in fetch_results)
            fetch_detail = f"{written_files} file(s)" if written_files else f"{len(fetch_results)} result(s)"
            if profile is not None:
                checklist.complete("Process tiles", detail=fetch_detail)
            else:
                checklist.complete("Fetch tiles", detail=fetch_detail)

            if src in ("viirs", "modis"):
                _report_fetch_writes(src, fetch_results, keep_processed=keep_processed, strategy=strategy)
            else:
                ok(f"Wrote {written_files} files")
                all_paths = [path for result in fetch_results for path in result.files]
                console.print(file_tree(src, all_paths))

            # Summarise peak-window / subsampling when active (VIIRS / MODIS / GFM)
            if src in ("viirs", "modis", "gfm") and (
                effective_days_before > 0 or effective_days_after > 0 or max_observations > 0
            ):
                n_returned = len(fetch_results)
                parts = []
                peak_label = ""
                if fetcher._peak_token:
                    peak_label = _date_label_from_token(fetcher._peak_token)
                if effective_days_before > 0 or effective_days_after > 0:
                    window_desc = f"window: -{effective_days_before}/+{effective_days_after} days around peak"
                    if peak_label:
                        window_desc += f" ({peak_label})"
                    parts.append(window_desc)
                if max_observations > 0:
                    parts.append(f"max {max_observations} obs (priority={peak_priority})")
                info(f"Peak filter applied — {n_returned} result(s) returned. {'; '.join(parts)}")

            # ── Optional plot + harmonise (all sources) ───────────────────
            _emit_source_outputs(
                src=src,
                fetcher=fetcher,
                fetch_results=fetch_results,
                strategy=strategy,
                event=event,
                output_dir=output_dir,
                plot_dir=plot_dir,
                plot=plot,
                harmonise=harmonise,
                checklist=checklist,
            )


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
        help="Emit the VIIRS derived layers from the registry-backed layer catalogue. Default: on."
        " Use --no-classify to emit the native VIIRS band untouched. See `atlantis list-layers`.",
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
        classify: If True, write water/flood-fraction plus reference/exclusion layers instead of raw data.
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
            step_names = _fetch_step_names("viirs")
            if plot:
                step_names.append("Plot outputs")
            if actual_harmonise:
                step_names.append("Harmonise outputs")

            with task_checklist(step_names, verbose=_VERBOSE_OUTPUT) as checklist:
                try:
                    with checklist.step("Process tiles", profile="viirs_fetch", pre_step="Fetch tiles"):
                        fetch_results = fetcher.fetch(event, event_viirs_dir)
                except Exception as exc:  # pragma: no cover - exercised in real fetch runs
                    failures.append((event.event_id, str(exc)))
                    summary_rows.append([event.event_id, "[red]✗ failed[/red]", "0", "—", "—"])
                    progress.advance(task)
                    continue

                _report_viirs_fetch_writes(fetch_results, keep_processed=keep_processed)
                written = sum(len(result.files) for result in fetch_results)
                total_files += written
                has_in_memory = any(result.dataset is not None for result in fetch_results)
                if written == 0 and not has_in_memory:
                    checklist.warn("Fetch tiles", detail="no files")
                    warn("No VIIRS files found for this case")
                    summary_rows.append([event.event_id, "[yellow]⚠ empty[/yellow]", "0", "—", "—"])
                    progress.advance(task)
                    continue

                fetch_detail = f"{written} file(s)" if written else f"{len(fetch_results)} result(s)"
                checklist.complete("Process tiles", detail=fetch_detail)

                peak_label = "—"
                harmonised_label = "—"

                # ── Per-date stats + best-date selection ──────────────────────
                if plot or actual_harmonise:
                    best_result, best_date_label = _select_best_result(fetcher, fetch_results)
                    best_ds = fetcher.to_dataset(best_result)
                    peak_label = best_date_label
                    png_dir = plot_dir or (event_viirs_dir / "plots")

                    if plot:
                        with checklist.step("Plot outputs"):
                            png_path = png_dir / f"{event.event_id}_{best_date_label}_viirs.png"
                            _plot_viirs(best_ds, event.event_id, best_date_label, output_png_path=png_path)
                        checklist.complete("Plot outputs", detail=best_date_label)

                    if actual_harmonise:
                        with checklist.step("Harmonise outputs"):
                            harm_dir = event_viirs_dir / "harmonised"
                            _harmonise_viirs(
                                best_ds,
                                event.event_id,
                                best_date_label,
                                harm_dir=harm_dir,
                                plot_dir=png_dir,
                            )
                        checklist.complete("Harmonise outputs", detail=best_date_label)
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


@cli.command("fetch-kurosiwo-modis")
def fetch_kurosiwo_modis(
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
    output_dir: Path | None = typer.Option(None, "--output", "-o", help="Output directory for MODIS products"),
    days_before: int = typer.Option(
        0,
        "--days-before",
        help="Days before KuroSiwo date_end to include in the MODIS search window",
    ),
    days_after: int = typer.Option(
        0,
        "--days-after",
        help="Days after KuroSiwo date_end to include in the MODIS search window",
    ),
    use_metadata_range: bool = typer.Option(
        False,
        "--use-metadata-range",
        help="Use date_start..date_end from the metadata CSV instead of a narrow window around date_end",
    ),
    modis_backend: str = typer.Option(
        "lance_geotiff",
        "--modis-backend",
        help="MODIS backend: lance_geotiff (streamable, ~1-week NRT) or laads_hdf4 (download, 2003+).",
    ),
    modis_composite: str = typer.Option(
        "F2",
        "--modis-composite",
        help="MODIS composite: F1, F1C, F2, F3. Default: F2 (2-day max-water composite).",
    ),
    classify: bool = typer.Option(
        True,
        "--classify/--no-classify",
        help="Emit the MODIS derived layers from the registry-backed layer catalogue,"
        " or --no-classify to emit the native MCDWD composite untouched."
        " See `atlantis list-layers` for the exact inventory.",
    ),
    stream: bool = typer.Option(
        True,
        "--stream/--no-stream",
        help="Stream remote tiles via /vsicurl/. Only valid with --modis-backend lance_geotiff.",
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
    """Fetch MODIS data for KuroSiwo cases.

    Args:
        metadata_path: Optional precomputed metadata CSV path.
        catalogue_path: KuroSiwo GeoPackage catalogue path used when metadata CSV is omitted.
        case: Only fetch one KuroSiwo flood case.
        limit: Limit the number of cases after filtering.
        output_dir: Output directory for MODIS products.
        days_before: Days before the KuroSiwo flood date to search.
        days_after: Days after the KuroSiwo flood date to search.
        use_metadata_range: Use the full metadata temporal range instead of a narrow flood-date window.
        modis_backend: Which MODIS backend to use (lance_geotiff or laads_hdf4).
        modis_composite: Which MCDWD composite to fetch (F1, F1C, F2, F3).
        classify: If True, write water_fraction/flood_fraction/reference_water/exclusion_mask/recurring_flood layers.
        stream: If True, stream remote tiles without downloading (lance_geotiff only).
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

    fetcher_cls = get_fetcher("modis")
    effective_stream = stream and modis_backend == "lance_geotiff"
    fetcher = fetcher_cls(
        backend=modis_backend,
        composite=modis_composite,
        classify=classify,
        stream=effective_stream,
        keep_processed=keep_processed,
    )

    command_header(
        "fetch-kurosiwo-modis",
        subtitle=f"{len(events)} case(s) · backend={modis_backend} · composite={modis_composite}",
    )
    console.print(f"[bold]KuroSiwo metadata:[/bold] {metadata_source_label}")
    console.print(f"[bold]Cases selected:[/bold] {len(events)}")
    console.print(f"[bold]Output root:[/bold] {output_root}")

    total_files = 0
    failures: list[tuple[str, str]] = []
    summary_rows: list[list[str]] = []

    with make_progress() as progress:
        task = progress.add_task("[cyan]Cases[/cyan]", total=len(events))

        for event in events:
            progress.console.print(
                f"\n[cyan]Fetching {event.event_id}[/cyan] "
                f"({event.start_date.isoformat()} → {event.end_date.isoformat()})"
            )
            event_modis_dir = output_root / event.event_id / "modis"
            step_names = _fetch_step_names("modis")
            if plot:
                step_names.append("Plot outputs")
            if harmonise:
                step_names.append("Harmonise outputs")

            with task_checklist(step_names, verbose=_VERBOSE_OUTPUT) as checklist:
                try:
                    with checklist.step("Process tiles", profile="modis_fetch", pre_step="Fetch tiles"):
                        fetch_results = fetcher.fetch(event, event_modis_dir)
                except Exception as exc:  # pragma: no cover - exercised in real fetch runs
                    failures.append((event.event_id, str(exc)))
                    summary_rows.append([event.event_id, "[red]✗ failed[/red]", "0", "—", "—"])
                    progress.advance(task)
                    continue

                _report_fetch_writes("modis", fetch_results, keep_processed=keep_processed)
                written = sum(len(result.files) for result in fetch_results)
                total_files += written
                has_in_memory = any(result.dataset is not None for result in fetch_results)
                if written == 0 and not has_in_memory:
                    checklist.warn("Fetch tiles", detail="no files")
                    warn("No MODIS files found for this case")
                    summary_rows.append([event.event_id, "[yellow]⚠ empty[/yellow]", "0", "—", "—"])
                    progress.advance(task)
                    continue

                fetch_detail = f"{written} file(s)" if written else f"{len(fetch_results)} result(s)"
                checklist.complete("Process tiles", detail=fetch_detail)

                peak_label = "—"
                harmonised_label = "—"

                if plot or harmonise:
                    best_result, best_date_label = _select_best_result(fetcher, fetch_results)
                    best_ds = fetcher.to_dataset(best_result)
                    peak_label = best_date_label
                    png_dir = plot_dir or (event_modis_dir / "plots")

                    if plot:
                        with checklist.step("Plot outputs"):
                            png_path = png_dir / f"{event.event_id}_{best_date_label}_modis.png"
                            _plot_source(
                                best_ds,
                                event.event_id,
                                best_date_label,
                                source_id="modis",
                                output_png_path=png_path,
                            )
                        checklist.complete("Plot outputs", detail=best_date_label)

                    if harmonise:
                        with checklist.step("Harmonise outputs"):
                            harm_dir = event_modis_dir / "harmonised"
                            _harmonise_source(
                                best_ds,
                                event.event_id,
                                best_date_label,
                                source_id="modis",
                                harm_dir=harm_dir,
                                plot_dir=png_dir,
                            )
                        checklist.complete("Harmonise outputs", detail=best_date_label)
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
                "KuroSiwo MODIS — run summary",
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
    """Harmonise fetched data (reproject + normalise) to target resolution.

    Reads processed GeoTIFFs from the fetcher output directory, reprojects
    them to a uniform 1 arcmin grid, preserving classified flood-fraction
    values on the 0-1 scale,
    and writes the harmonised GeoTIFFs. Supports the ``viirs`` and ``modis``
    sources (file names follow the ``{event}_{date}_{source}_{layer}.tif``
    convention).
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
            if source in ("viirs", "modis"):
                cfg.variable_resampling["flood_fraction"] = resampling
        harmoniser = Harmoniser(config=cfg)
    else:
        harmoniser = Harmoniser()

    output_path = output_root / source / "harmonised"
    output_path.mkdir(parents=True, exist_ok=True)

    resolution_str = f"{harmoniser.config.target_resolution_arcmin} arcmin"
    command_header("harmonise", subtitle=f"{event} · {source} · {resolution_str}")
    import rioxarray as rxr

    processed_dir: Path | None = None
    tif_files: list[Path] = []
    phase_names = ["Discover inputs", "Preview outputs" if dry_run else "Harmonise files"]

    with task_checklist(phase_names, verbose=_VERBOSE_OUTPUT) as checklist:
        with checklist.step("Discover inputs"):
            # Search strategy: try the standard layout first, then rglob broadly
            for root in (input_root, Path.cwd() / "scripts" / "data", Path.cwd()):
                if not root.exists():
                    continue
                candidate = root / source / "processed"
                if candidate.exists():
                    hits = sorted(candidate.glob(f"{event}_*_{source}_flood_fraction.tif"))
                    if not hits:
                        hits = sorted(candidate.glob(f"{event}_*_{source}_raw.tif"))
                    if hits:
                        processed_dir, tif_files = candidate, hits
                        break

                hits = sorted(root.glob(f"{event}_*_{source}_flood_fraction.tif"))
                if not hits:
                    hits = sorted(root.glob(f"{event}_*_{source}_raw.tif"))
                if hits:
                    processed_dir, tif_files = root, hits
                    break

                for vdir in sorted(root.rglob(f"{source}/processed/")):
                    hits = sorted(vdir.glob(f"{event}_*_{source}_flood_fraction.tif"))
                    if not hits:
                        hits = sorted(vdir.glob(f"{event}_*_{source}_raw.tif"))
                    if hits:
                        processed_dir, tif_files = vdir, hits
                        break
                if tif_files:
                    break

        if not tif_files:
            checklist.fail("Discover inputs", detail="no processed files")
            fail(f"No processed {_pretty_source(source)} files found matching '{event}'")
            console.print("  Tried: cache dir, scripts/data/, and repository root.")
            console.print("  Run 'atlantis fetch' first, or use --input to point to existing data.")
            raise typer.Exit(code=1)

        checklist.complete("Discover inputs", detail=f"{len(tif_files)} file(s)")

        console.print(f"[bold]Harmonising {len(tif_files)} file(s)[/bold]")
        console.print(f"[bold]Input:[/bold] {processed_dir}")
        console.print(f"[bold]Output:[/bold] {output_path}")
        console.print(f"[bold]Target resolution:[/bold] {resolution_str} ({harmoniser.config.target_resolution:.8f}°)")
        console.print(f"[bold]Resampling:[/bold] {harmoniser.config.resampling}")

        if dry_run:
            with checklist.step("Preview outputs"):
                dry_rows = []
                for tf in tif_files:
                    stem = tf.stem.replace("flood_fraction", "harmonised").replace("raw", "harmonised")
                    out = output_path / f"{stem}.tif"
                    dry_rows.append([tf.name, out.name])
                console.print(summary_table("Dry run — would process", ["Input file", "Output file"], dry_rows))
            checklist.complete("Preview outputs", detail=f"{len(dry_rows)} file(s)")
            return

        harmonised_count = 0
        with checklist.step("Harmonise files"):
            with make_progress() as progress:
                task = progress.add_task("[cyan]Harmonising[/cyan]", total=len(tif_files))
                for tif_path in tif_files:
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
        checklist.complete("Harmonise files", detail=f"{harmonised_count} file(s)")

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
        "modis": "MODIS MCDWD Flood Detection (NASA LANCE / LAADS)",
        "rfm": "Regional Flood Model (Phase C)",
    }
    rows = [[src, source_descriptions.get(src, "No description")] for src in sources]
    console.print(summary_table("Available Data Sources", ["Name", "Description"], rows))


@cli.command("list-layers")
def list_layers_cmd(
    source: str | None = typer.Option(
        None,
        "--source",
        "-s",
        help="Limit to one source (e.g. modis, viirs, gfm). Omit to list every source.",
    ),
) -> None:
    """List the native and derived layers available per source."""
    from atlantis.layers import all_registries, available_sources, get_source_registry

    command_header("list-layers")

    if source is not None:
        available = available_sources()
        if source not in available:
            fail(f"Unknown source '{source}'. Available: {', '.join(available)}")
            raise typer.Exit(code=1)
        registries = {source: get_source_registry(source)}
    else:
        registries = all_registries()

    for source_id, registry in registries.items():
        section_rule(f"{source_id} layers")
        rows = [
            [layer.name, "native", layer.dtype, str(layer.nodata), layer.description]
            for layer in registry.list_native()
        ]
        rows += [
            [layer.name, "derived", layer.dtype, str(layer.nodata), layer.description]
            for layer in registry.list_derived()
        ]
        console.print(
            summary_table(
                f"{source_id} layers",
                ["Layer", "Kind", "dtype", "nodata", "Description"],
                rows,
            )
        )


@cli.command()
def setup(
    check_only: bool = typer.Option(
        False,
        "--check-only",
        help="Only verify assets/credentials are present without modifying anything.",
    ),
    update_hashes: bool = typer.Option(
        False,
        "--update-hashes",
        help="Recompute SHA-256 hashes and write them to config/asset_hashes.json.",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Never prompt for missing credentials (default: prompt when stdin is a TTY).",
    ),
    verify_aws: bool = typer.Option(
        False,
        "--verify-aws",
        help="After the standard checks, run a live round-trip S3 list against each AWS profile.",
    ),
) -> None:
    """Bootstrap required data assets and credentials.

    Restores missing tracked files from git, verifies SHA-256 hashes, and
    prompts (interactively) for any missing credentials such as
    ``EARTHDATA_TOKEN`` (used by the MODIS fetcher). Run this once after
    cloning, or whenever a new data source is added.

    Use ``--verify-aws`` to additionally make a single S3 ``ListObjectsV2``
    call against each registered AWS profile to confirm credentials and
    endpoints actually work.
    """
    from atlantis.utils.setup import run_setup, verify_aws_profiles

    command_header("setup")
    interactive: bool | None = False if non_interactive else None
    success = run_setup(
        auto_fix=not check_only,
        output=console,
        update_hashes=update_hashes,
        interactive=interactive,
    )
    if verify_aws:
        success = verify_aws_profiles(output_print=console.print) and success
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

    step_names = ["Fetch tiles", "Process tiles", "Plot outputs"]
    if harmonise:
        step_names.append("Harmonise outputs")

    with task_checklist(step_names, verbose=_VERBOSE_OUTPUT) as checklist:
        with checklist.step("Process tiles", profile="viirs_fetch", pre_step="Fetch tiles"):
            fetch_results = fetcher.fetch(event, viirs_dir)
        if not fetch_results:
            checklist.warn("Fetch tiles", detail="no files")
            warn("No VIIRS data found for this region/date range.")
            raise typer.Exit(code=1)

        checklist.complete("Process tiles", detail=f"{sum(len(result.files) for result in fetch_results)} file(s)")

        best_result, best_date_label = _select_best_result(fetcher, fetch_results)
        best_ds = fetcher.to_dataset(best_result)

        plot_dir_path = viirs_dir / "plots"
        plot_dir_path.mkdir(parents=True, exist_ok=True)
        png_path = plot_dir_path / f"Valencia_2024_{best_date_label}_viirs.png"
        with checklist.step("Plot outputs"):
            _plot_viirs(best_ds, "Valencia_2024", best_date_label, output_png_path=png_path, announce=False)
        checklist.complete("Plot outputs", detail=best_date_label)

        if harmonise:
            harm_dir = viirs_dir / "harmonised"
            with checklist.step("Harmonise outputs"):
                _harmonise_viirs(
                    best_ds,
                    "Valencia_2024",
                    best_date_label,
                    harm_dir=harm_dir,
                    plot_dir=plot_dir_path,
                    announce=False,
                )
            checklist.complete("Harmonise outputs", detail=best_date_label)

    console.print("")
    ok("Demo complete!")
    from atlantis.utils.ui import file_tree as _ft

    output_files = [path for result in fetch_results for path in result.files]
    if harmonise:
        output_files.extend(
            [
                png_path,
                harm_dir / f"Valencia_2024_{best_date_label}_viirs_harmonised.tif",
                plot_dir_path / f"Valencia_2024_{best_date_label}_viirs_harmonised.png",
            ]
        )
    else:
        output_files.append(png_path)
    console.print(_ft(str(out), output_files))


@cli.command("demo-modis")
def demo_modis(
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
        help="Stream remote tiles (only for lance_geotiff backend). Default: on.",
    ),
    modis_backend: str = typer.Option(
        "laads_hdf4",
        "--modis-backend",
        help="MODIS backend: laads_hdf4 (download, 2003+) or lance_geotiff (streamable, ~1-week NRT only).",
    ),
    modis_composite: str = typer.Option(
        "F2",
        "--modis-composite",
        help="MODIS composite: F1, F1C, F2, F3. Default: F2 (2-day max-water composite).",
    ),
) -> None:
    """Run the Valencia 2024 flood demo with MODIS data.

    Fetches MODIS data for the Valencia flood event (Oct–Nov 2024),
    plots the peak-flood date, and optionally harmonises to 1 arcmin.
    A quick way to verify that MODIS fetching works end-to-end.

    Note: the default backend is laads_hdf4 because Valencia 2024 is a
    historical event outside the ~1-week LANCE NRT window. This backend
    requires EARTHDATA_TOKEN (run `uv run atlantis setup` once).
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

    command_header("demo-modis", subtitle="Valencia 2024 flood — MODIS")

    event = FloodEvent(
        event_id="Valencia_2024",
        bbox=(-1.5, 38.8, 0.5, 40.0),
        start_date=date(2024, 10, 29),
        end_date=date(2024, 11, 4),
    )

    effective_stream = stream and modis_backend == "lance_geotiff"
    fetcher_cls = get_fetcher("modis")
    fetcher = fetcher_cls(
        backend=modis_backend,
        composite=modis_composite,
        classify=True,
        stream=effective_stream,
        keep_processed=True,
    )

    modis_dir = out / "modis"

    console.print(f"[bold]Event:[/bold] {event.event_id}")
    console.print(f"[bold]BBox:[/bold]  {event.bbox}")
    console.print(f"[bold]Dates:[/bold] {event.start_date} → {event.end_date}")
    console.print(f"[bold]Backend:[/bold] {modis_backend} (composite={modis_composite})")
    console.print(f"[bold]Output:[/bold] {out}\n")

    try:
        with step_status("Fetching MODIS tiles…"):
            fetch_results = fetcher.fetch(event, modis_dir)
    except requests.RequestException as exc:
        fail(f"Network error while fetching MODIS: {exc}")
        if modis_backend == "laads_hdf4":
            info("Hint: LAADS requires EARTHDATA_TOKEN. Run `uv run atlantis setup` to configure.")
        raise typer.Exit(code=1)
    if not fetch_results:
        warn("No MODIS data found for this region/date range.")
        if modis_backend == "lance_geotiff":
            info(
                "Hint: lance_geotiff only covers a ~1-week NRT window. "
                "Use --modis-backend laads_hdf4 for historical events."
            )
        raise typer.Exit(code=1)

    _report_fetch_writes("modis", fetch_results, keep_processed=True)

    # Select best date
    best_result, best_date_label = _select_best_result(fetcher, fetch_results)
    best_ds = fetcher.to_dataset(best_result)

    # Plot
    plot_dir_path = modis_dir / "plots"
    plot_dir_path.mkdir(parents=True, exist_ok=True)
    png_path = plot_dir_path / f"Valencia_2024_{best_date_label}_modis.png"
    with step_status("Plotting peak-flood date…"):
        _plot_source(best_ds, "Valencia_2024", best_date_label, source_id="modis", output_png_path=png_path)

    # Harmonise
    if harmonise:
        harm_dir = modis_dir / "harmonised"
        with step_status("Harmonising to 1 arcmin…"):
            _harmonise_source(
                best_ds,
                "Valencia_2024",
                best_date_label,
                source_id="modis",
                harm_dir=harm_dir,
                plot_dir=plot_dir_path,
            )

    console.print("")
    ok("Demo complete!")
    from atlantis.utils.ui import file_tree as _ft

    if harmonise:
        output_files = [
            png_path,
            harm_dir / f"Valencia_2024_{best_date_label}_modis_harmonised.tif",
            plot_dir_path / f"Valencia_2024_{best_date_label}_modis_harmonised.png",
        ]
    else:
        output_files = [png_path]
    console.print(_ft(str(out), output_files))


@cli.command("demo-gfm")
def demo_gfm(
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
) -> None:
    """Run the Valencia 2024 flood demo with GFM (Sentinel-1 SAR) data.

    Fetches GFM data for the Valencia flood event (Oct–Nov 2024),
    plots the peak-flood date, and optionally harmonises to 1 arcmin.
    A quick way to verify that GFM fetching works end-to-end.
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

    command_header("demo-gfm", subtitle="Valencia 2024 flood — GFM")

    event = FloodEvent(
        event_id="Valencia_2024",
        bbox=(-1.5, 38.8, 0.5, 40.0),
        start_date=date(2024, 10, 29),
        end_date=date(2024, 11, 4),
    )

    fetcher_cls = get_fetcher("gfm")
    fetcher = fetcher_cls(
        strategy="peak",
        keep_processed=True,
    )

    gfm_dir = out / "gfm"

    console.print(f"[bold]Event:[/bold] {event.event_id}")
    console.print(f"[bold]BBox:[/bold]  {event.bbox}")
    console.print(f"[bold]Dates:[/bold] {event.start_date} → {event.end_date}")
    console.print(f"[bold]Output:[/bold] {out}\n")

    try:
        with step_status("Fetching GFM tiles…"):
            fetch_results = fetcher.fetch(event, gfm_dir)
    except (requests.RequestException, Exception) as exc:
        if "SSL" in str(exc) or "ConnectionError" in type(exc).__name__:
            fail(f"Network error while fetching GFM: {exc}")
            info(
                "Hint: the EODC STAC API (stac.eodc.eu) may be unreachable due to "
                "SSL/proxy issues. Try setting REQUESTS_CA_BUNDLE or SSL_CERT_FILE "
                "to your corporate CA bundle."
            )
            raise typer.Exit(code=1)
        raise
    if not fetch_results:
        warn("No GFM data found for this region/date range.")
        raise typer.Exit(code=1)

    _report_fetch_writes("gfm", fetch_results, keep_processed=True)

    # Select best date
    best_result, best_date_label = _select_best_result(fetcher, fetch_results)
    best_ds = fetcher.to_dataset(best_result)

    # Plot
    plot_dir_path = gfm_dir / "plots"
    plot_dir_path.mkdir(parents=True, exist_ok=True)
    png_path = plot_dir_path / f"Valencia_2024_{best_date_label}_gfm.png"
    with step_status("Plotting peak-flood date…"):
        _plot_source(best_ds, "Valencia_2024", best_date_label, source_id="gfm", output_png_path=png_path)

    # Harmonise
    if harmonise:
        harm_dir = gfm_dir / "harmonised"
        with step_status("Harmonising to 1 arcmin…"):
            _harmonise_source(
                best_ds,
                "Valencia_2024",
                best_date_label,
                source_id="gfm",
                harm_dir=harm_dir,
                plot_dir=plot_dir_path,
            )

    console.print("")
    ok("Demo complete!")
    from atlantis.utils.ui import file_tree as _ft

    if harmonise:
        output_files = [
            png_path,
            harm_dir / f"Valencia_2024_{best_date_label}_gfm_harmonised.tif",
            plot_dir_path / f"Valencia_2024_{best_date_label}_gfm_harmonised.png",
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


# ── Batch subcommand ──────────────────────────────────────────────────────────

batch_app = typer.Typer(help="Batch-process satellite datasets to s3://atlantis/.")
cli.add_typer(batch_app, name="batch")

viirs_batch_app = typer.Typer(help="Batch-process VIIRS granules to 1-arcmin COGs.")
batch_app.add_typer(viirs_batch_app, name="viirs")


@viirs_batch_app.command("run")
def batch_viirs(
    inventory: str = typer.Option(
        "s3://atlantis/assets/viirs/jpss/2020/catalogue.parquet",
        "--inventory",
        help="Path or S3 URI to the VIIRS JPSS catalogue Parquet file.",
    ),
    output: str = typer.Option(
        "s3://atlantis/viirs/jpss/2020/",
        "--output",
        help="S3 prefix for output COGs (must start with s3://atlantis/).",
    ),
    partition: str | None = typer.Option(
        None,
        "--partition",
        help="Row slice of the catalogue to process, e.g. '0:24464'. None = full catalogue.",
    ),
    workers_min: int = typer.Option(2, "--workers-min", help="Minimum Dask worker processes."),
    workers_max: int = typer.Option(6, "--workers-max", help="Maximum Dask worker processes (adaptive)."),
    memory_limit: str = typer.Option("6GB", "--memory-limit", help="Memory cap per worker."),
    dashboard_port: int = typer.Option(8787, "--dashboard-port", help="Dask dashboard port."),
    db_path: Path = typer.Option(Path("tracker.db"), "--db-path", help="SQLite resume database path."),
    retries: int = typer.Option(3, "--retries", help="Dask retry count per granule."),
    log_every: int = typer.Option(100, "--log-every", help="Log a progress line every N completions."),
) -> None:
    """Batch-process VIIRS JPSS 2020 granules → 1-arcmin uint8 flood-fraction COGs.

    Reads the catalogue from --inventory, converts each row to a task, and
    runs all tasks through a Dask LocalCluster.  Progress is persisted in
    --db-path so the run can be safely interrupted and resumed.

    Two-VM split example:

        VM1: atlantis batch viirs run --partition 0:24464  --db-path tracker_vm1.db
        VM2: atlantis batch viirs run --partition 24464:48928 --db-path tracker_vm2.db
    """
    from atlantis.batch import BatchConfig, run_batch
    from atlantis.fetchers.viirs.batch_processor import process_granule
    from atlantis.fetchers.viirs.inventory import load_inventory, slice_partition, to_tasks
    from atlantis.utils.setup import AWS_PROFILES

    command_header("batch viirs")

    # ── Pre-flight checks ─────────────────────────────────────────────────
    if not output.startswith("s3://atlantis/"):
        fail("--output must start with 's3://atlantis/'")
        raise typer.Exit(code=1)

    ecmwf_profile = next((p for p in AWS_PROFILES if p.name == "default"), None)
    if ecmwf_profile is None or not ecmwf_profile.endpoint_url:
        fail("The 'default' AWS profile is not configured. Run `atlantis setup` first.")
        raise typer.Exit(code=1)

    # ── Load & slice catalogue ────────────────────────────────────────────
    info(f"Loading catalogue from {inventory} …")
    df = load_inventory(inventory)
    df = slice_partition(df, partition)
    tasks = to_tasks(df, output_prefix=output.removeprefix("s3://atlantis/").strip("/"))

    console.print(
        f"  [bold]{len(tasks)}[/bold] granules to process" + (f"  (partition {partition})" if partition else "")
    )

    cfg = BatchConfig(
        db_path=db_path,
        workers_min=workers_min,
        workers_max=workers_max,
        memory_limit_per_worker=memory_limit,
        dashboard_port=dashboard_port,
        retries=retries,
        log_every=log_every,
    )

    # ── Run ───────────────────────────────────────────────────────────────
    run_batch(tasks, process_fn=process_granule, cfg=cfg)


if __name__ == "__main__":
    cli()
