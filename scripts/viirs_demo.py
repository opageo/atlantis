r"""VIIRS fetch and visualisation demo.

Showcases two independent workflows using the Atlantis Python API:

1. **Arbitrary event** — fetch + visualise VIIRS for any user-supplied bbox
   and date range (no KuroSiwo catalogue needed).

2. **KuroSiwo event** — fetch + visualise VIIRS for a random (or specified)
   event from the KuroSiwo catalogue, using the pre-built metadata CSV.

Visualisations are saved as PNG files under ``scripts/`` (tracked).
Downloaded GeoTIFFs are written under ``scripts/data/`` (untracked).

.. note::

    The VIIRS fetcher depends on a global AOI tile grid.  Run the one-time
    setup before using this demo::

        uv run python scripts/setup.py

Usage::

    # Arbitrary bbox + date range — Valencia 2024 flood example
    uv run python scripts/viirs_demo.py arbitrary \
        --event-id valencia_2024 \
        --bbox "-1.2 39.0 0.2 39.8" \
        --start-date 2024-10-30 \
        --end-date 2024-11-01

    # KuroSiwo event — bbox and dates resolved from the catalogue automatically
    uv run python scripts/viirs_demo.py kurosiwo --ks-case KuroSiwo_470

    # Stream tiles, harmonise to 1 arcmin, use inclusive flood threshold
    uv run python scripts/viirs_demo.py kurosiwo \
        --ks-case KuroSiwo_1111004 --stream --harmonise \
        --days-before 1 --days-after 1 --flood-threshold 101

.. tip::

    The same data operations are available directly via the ``atlantis`` CLI.

    Arbitrary event (Valencia 2024 flood)::

        uv run atlantis fetch \
            --event valencia_2024 --source viirs \
            --bbox "-1.2 39.0 0.2 39.8" \
            --start-date 2024-10-30 --end-date 2024-11-01 \
            --classify --stream --plot --harmonise

    KuroSiwo event::

        uv run atlantis fetch-kurosiwo-viirs \
            --case KuroSiwo_1111004 --classify --stream \
            --plot --harmonise --days-before 1 --days-after 1
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
import typer

# ── repo root on sys.path so the script works from any cwd ───────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

# ── guard: the VIIRS AOI grid must exist before either demo mode works ───────
_AOI_GRID = _REPO_ROOT / "src" / "atlantis" / "fetchers" / "viirs" / "data" / "viirs_aois.geojson"
if not _AOI_GRID.exists():
    sys.exit(
        "The VIIRS AOI tile grid is missing.\n"
        f"Expected: {_AOI_GRID.relative_to(_REPO_ROOT)}\n\n"
        "Run once to bootstrap it:\n"
        "  uv run python scripts/setup.py"
    )

from atlantis.fetchers.viirs import VIIRSFetcher  # noqa: E402
from atlantis.models.event import FloodEvent  # noqa: E402
from atlantis.utils.kurosiwo import (  # noqa: E402
    KUROSIWO_DEFAULT_CATALOGUE,
    KUROSIWO_DEFAULT_METADATA,
    build_kurosiwo_flood_events,
    build_kurosiwo_flood_events_from_catalogue,
)
from atlantis.utils.plot import (  # noqa: E402
    date_from_filename,
    pixel_stats_classified,
    pixel_stats_raw,
    plot_classified,
    plot_raw,
)

SCRIPTS_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPTS_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Shared fetch-visualise pipeline
# ─────────────────────────────────────────────────────────────────────────────


def _fetch_and_visualise(
    event: FloodEvent,
    output_dir: Path,
    png_path: Path,
    *,
    stream: bool,
    classify: bool,
    harmonise: bool,
    flood_threshold: int,
    harmonised_png_path: Path,
) -> None:
    """Run the full fetch → stats → best-date plot → optional harmonise pipeline."""
    fetcher = VIIRSFetcher(
        classify=classify,
        stream=stream,
        strategy="peak",
        keep_processed=True,
    )
    # Note: flood_min_code is not a direct init arg for VIIRSFetcher,
    # it's usually handled in the processor or via config.
    # I'll leave it as is if it was working before, but check if it needs adjustment.

    search_results = fetcher.search(event)
    print(f"  Found {len(search_results)} VIIRS tile(s) in the NOAA S3 archive")
    if not search_results:
        print("  No tiles found — try a different date, bbox, or increase --days-before/--days-after")
        return

    fetch_results = fetcher.fetch(event, output_dir)
    if not fetch_results:
        print("  Fetch returned no results — nothing to plot")
        return

    print(f"  Fetched {sum(len(r.files) for r in fetch_results)} file(s):")
    for result in fetch_results:
        for path in result.files:
            print(f"    {path.relative_to(_REPO_ROOT)}")

    # ── Select the date with the most flood pixels ────────────────────────
    best_result = fetch_results[0]
    best_date = date_from_filename(fetch_results[0].files[0].name)
    best_flood_count = 0

    for result in fetch_results:
        ds = fetcher.to_dataset(result)
        dlabel = date_from_filename(result.files[0].name)
        print(f"\n  {dlabel}")
        if "flood_extent" in ds:
            flooded = pixel_stats_classified(ds["flood_extent"].values)
            if flooded > best_flood_count:
                best_flood_count = flooded
                best_result = result
                best_date = dlabel
        else:
            pixel_stats_raw(ds["raw"].values)

    best_ds = fetcher.to_dataset(best_result)
    print(f"\n  Plotting peak-flood date: {best_date} ({best_flood_count:,} flooded px)")

    if "flood_extent" in best_ds:
        plot_classified(
            best_ds["flood_extent"],
            title=f"{event.event_id}: VIIRS flood extent {best_date} (375 m, threshold={flood_threshold})",
            output_path=png_path,
        )
    else:
        plot_raw(
            best_ds["raw"],
            title=f"{event.event_id}: VIIRS raw composite {best_date} (375 m)",
            output_path=png_path,
        )

    # ── Optional harmonise ────────────────────────────────────────────────
    if harmonise:
        from atlantis.harmoniser import Harmoniser

        harm_dir = output_dir / "harmonised"
        harm_dir.mkdir(parents=True, exist_ok=True)
        h = Harmoniser()
        ds_harm = h.harmonise(best_ds, source_id="viirs")
        flood_var = "flood_extent" if "flood_extent" in ds_harm else list(ds_harm.data_vars)[0]
        tif_path = harm_dir / f"{event.event_id}_{best_date}_viirs_harmonised.tif"
        ds_harm[flood_var].rio.to_raster(str(tif_path), dtype="float32", compress="LZW", nodata=float("nan"))
        print(f"  Harmonised GeoTIFF: {tif_path.relative_to(_REPO_ROOT)}")
        pixel_stats_classified(ds_harm[flood_var].values, name="flood fraction (1 arcmin)")
        plot_classified(
            ds_harm[flood_var],
            title=f"{event.event_id}: VIIRS harmonised {best_date} (1 arcmin)",
            output_path=harmonised_png_path,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Typer application
# ─────────────────────────────────────────────────────────────────────────────

app = typer.Typer(help="VIIRS fetch and visualisation demo.")

_FLOOD_THRESHOLD_OPTION = typer.Option(
    101,
    "--flood-threshold",
    min=101,
    max=200,
    help="Minimum VIIRS pixel code for flood (101=most inclusive, 200=most conservative). Default: 101 (all flood).",
)


@app.command()
def arbitrary(
    event_id: str = typer.Option("valencia_2024", "--event-id", help="Label for output file names."),
    bbox: str = typer.Option(
        "-1.2 39.0 0.2 39.8",
        "--bbox",
        help="Bounding box: 'west south east north' in degrees.",
    ),
    start_date: str = typer.Option("2024-10-30", "--start-date", help="Start date (YYYY-MM-DD)."),
    end_date: str = typer.Option("2024-11-01", "--end-date", help="End date (YYYY-MM-DD)."),
    stream: bool = typer.Option(False, "--stream", help="Stream tiles via /vsicurl/ instead of downloading."),
    classify: bool = typer.Option(
        False,
        "--classify",
        help="Classify pixels into flood/quality/water layers (saves 3 files). Default: raw output.",
    ),
    harmonise: bool = typer.Option(False, "--harmonise", help="Resample output to 1 arcmin after fetching."),
    flood_threshold: int = _FLOOD_THRESHOLD_OPTION,
) -> None:
    """Fetch + visualise VIIRS for any user-defined bbox and date range."""
    print("\n" + "=" * 60)
    print("Arbitrary event — user-defined bbox + date range")
    print("=" * 60)

    parts = bbox.split()
    if len(parts) != 4:
        raise typer.BadParameter("--bbox must be four numbers: west south east north")
    w, s, e, n = (float(p) for p in parts)

    event = FloodEvent(
        event_id=event_id,
        bbox=(w, s, e, n),
        start_date=date.fromisoformat(start_date),
        end_date=date.fromisoformat(end_date),
    )
    output_dir = DATA_DIR / event.event_id / "viirs"
    print(f"  BBox: west={w}, south={s}, east={e}, north={n}")
    print(f"  Dates: {event.start_date} -> {event.end_date}")
    print(f"  Output: {output_dir.relative_to(_REPO_ROOT)}")

    _fetch_and_visualise(
        event,
        output_dir,
        png_path=SCRIPTS_DIR / "viirs_arbitrary_event.png",
        harmonised_png_path=SCRIPTS_DIR / "viirs_arbitrary_harmonised.png",
        stream=stream,
        classify=classify,
        harmonise=harmonise,
        flood_threshold=flood_threshold,
    )
    _done()


@app.command()
def kurosiwo(
    ks_case: Optional[str] = typer.Option(
        None,
        "--ks-case",
        help="KuroSiwo flood_case (e.g. KuroSiwo_470). Omit to pick a random case.",
    ),
    days_before: int = typer.Option(0, "--days-before", help="Days before the KuroSiwo flood date to include."),
    days_after: int = typer.Option(0, "--days-after", help="Days after the KuroSiwo flood date to include."),
    stream: bool = typer.Option(False, "--stream", help="Stream tiles via /vsicurl/ instead of downloading."),
    classify: bool = typer.Option(
        False,
        "--classify",
        help="Classify pixels into flood/quality/water layers (saves 3 files). Default: raw output.",
    ),
    harmonise: bool = typer.Option(False, "--harmonise", help="Resample output to 1 arcmin after fetching."),
    flood_threshold: int = _FLOOD_THRESHOLD_OPTION,
) -> None:
    """Fetch + visualise VIIRS for a KuroSiwo catalogue event."""
    print("\n" + "=" * 60)
    print("KuroSiwo event")
    print("=" * 60)

    if ks_case is None:
        ks_case = _random_ks_case()

    metadata_path = _REPO_ROOT / KUROSIWO_DEFAULT_METADATA
    catalogue_path = _REPO_ROOT / KUROSIWO_DEFAULT_CATALOGUE

    if metadata_path.exists():
        events = build_kurosiwo_flood_events(
            metadata_path,
            case=ks_case,
            days_before=days_before,
            days_after=days_after,
        )
    else:
        events = build_kurosiwo_flood_events_from_catalogue(
            catalogue_path,
            case=ks_case,
            days_before=days_before,
            days_after=days_after,
        )

    if not events:
        print(f"  No events found for case '{ks_case}' -- skipping")
        return

    event = events[0]
    output_dir = DATA_DIR / event.event_id / "viirs"
    print(f"  Case:   {event.event_id}")
    print(f"  BBox:   {event.bbox}")
    print(f"  Dates:  {event.start_date} -> {event.end_date}  (+-{days_before}/{days_after} days)")
    print(f"  Output: {output_dir.relative_to(_REPO_ROOT)}")

    _fetch_and_visualise(
        event,
        output_dir,
        png_path=SCRIPTS_DIR / "viirs_kurosiwo_event.png",
        harmonised_png_path=SCRIPTS_DIR / "viirs_kurosiwo_harmonised.png",
        stream=stream,
        classify=classify,
        harmonise=harmonise,
        flood_threshold=flood_threshold,
    )
    _done()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _random_ks_case() -> str:
    """Pick a random KuroSiwo flood_case from the metadata CSV or catalogue."""
    metadata_path = _REPO_ROOT / KUROSIWO_DEFAULT_METADATA
    if metadata_path.exists():
        df = pd.read_csv(metadata_path)
        case = str(df["flood_case"].dropna().sample(1, random_state=42).iloc[0])
        print(f"  Randomly selected case: {case}")
        return case
    import geopandas as gpd

    catalogue = gpd.read_file(_REPO_ROOT / KUROSIWO_DEFAULT_CATALOGUE)
    actid = int(catalogue["actid"].dropna().drop_duplicates().sample(1, random_state=42).iloc[0])
    case = f"KuroSiwo_{actid:03d}"
    print(f"  Randomly selected case: {case}")
    return case


def _done() -> None:
    print("\nDone.")
    print(f"  Visualisations: {SCRIPTS_DIR.relative_to(_REPO_ROOT)}/viirs_*.png")
    print(f"  Data:           {DATA_DIR.relative_to(_REPO_ROOT)}/")


if __name__ == "__main__":
    app()
