r"""GFM fetch and visualisation demo.

Showcases two independent workflows using the Atlantis Python API:

1. **Arbitrary event** — fetch + visualise GFM for any user-supplied bbox
   and date range. Queries the EODC STAC API for Sentinel-1 SAR flood
   extent data.

2. **KuroSiwo event** — fetch + visualise GFM for a random (or specified)
   event from the KuroSiwo catalogue, using the pre-built metadata CSV.

Visualisations are saved as PNG files under ``scripts/`` (tracked).
Downloaded GeoTIFFs are written under ``scripts/data/`` (untracked).

.. important::

    **GFM data is already on the canonical 1-arcmin EPSG:4326 grid.**

    Unlike VIIRS (375 m) and MODIS (250 m), the GFM processor reprojects
    Sentinel-1 SAR data directly onto the canonical 1-arcmin global grid
    during ``fetch()``. The ``flood_fraction`` output is therefore at the
    same resolution that VIIRS / MODIS reach only after ``--harmonise``.

    Running ``--harmonise`` on GFM output re-encodes ``float32 [0, 1]``
    to ``uint8 [0, 100]`` (nodata = 255) for parity with other sources
    — it does **not** change the spatial resolution.

.. note::

    No AOI grid bootstrap is needed for GFM (unlike VIIRS which requires
    ``uv run python scripts/setup.py``). Data is accessed via the EODC
    STAC API and streamed on-the-fly through ``odc.stac``.

Usage::

    # Arbitrary bbox + date range — Valencia 2024 flood example
    uv run python scripts/gfm_demo.py arbitrary \
        --event-id Valencia_2024 \
        --bbox "-1.5 38.8 0.5 40.0" \
        --start-date 2024-10-29 \
        --end-date 2024-11-04

    # KuroSiwo event — bbox and dates resolved from the catalogue
    uv run python scripts/gfm_demo.py kurosiwo --ks-case KuroSiwo_470

    # Harmonise to uint8 percentage, pick only the peak date
    uv run python scripts/gfm_demo.py arbitrary \
        --event-id Valencia_2024 \
        --bbox "-1.5 38.8 0.5 40.0" \
        --start-date 2024-10-29 \
        --end-date 2024-11-04 \
        --harmonise --strategy peak

.. tip::

    The same operations are available via the ``atlantis`` CLI::

        uv run atlantis fetch \
            --event Valencia_2024 --source gfm \
            --bbox "-1.5 38.8 0.5 40.0" \
            --start-date 2024-10-29 --end-date 2024-11-04 \
            --plot --harmonise
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

from atlantis.fetchers.gfm import GFMFetcher  # noqa: E402
from atlantis.harmoniser import write_harmonised_raster  # noqa: E402
from atlantis.models.event import FloodEvent  # noqa: E402
from atlantis.utils.kurosiwo import (  # noqa: E402
    KUROSIWO_DEFAULT_CATALOGUE,
    KUROSIWO_DEFAULT_METADATA,
    build_kurosiwo_flood_events,
    build_kurosiwo_flood_events_from_catalogue,
)
from atlantis.utils.plot import (  # noqa: E402
    pixel_stats_classified,
    plot_classified,
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
    strategy: str,
    coarsen_factor: int,
    harmonise: bool,
    harmonised_png_path: Path,
) -> None:
    """Run the full fetch → stats → best-date plot → optional harmonise pipeline.

    GFM data is loaded from the EODC STAC API via ``odc.stac`` and processed
    into ``flood_fraction``, ``quality_mask``, and ``permanent_water`` layers
    on the canonical 1-arcmin EPSG:4326 grid.
    """
    # ── Banner: explain the 1-arcmin output ───────────────────────────────
    print()
    print("  ┌──────────────────────────────────────────────────────────────┐")
    print("  │  GFM data is already on the canonical 1-arcmin EPSG:4326    │")
    print("  │  grid.  Unlike VIIRS (375 m) and MODIS (250 m), the fetched │")
    print("  │  flood_fraction raster needs no resampling.                  │")
    print("  │                                                              │")
    print("  │  --harmonise re-encodes float32 [0,1] → uint8 [0,100]       │")
    print("  │  (nodata=255) for parity with other sources — same grid.    │")
    print("  └──────────────────────────────────────────────────────────────┘")
    print()

    fetcher = GFMFetcher(
        strategy=strategy,
        coarsen_factor=coarsen_factor,
        keep_processed=True,
    )

    search_results = fetcher.search(event)
    print(f"  Found {len(search_results)} GFM STAC item(s) on EODC")
    if not search_results:
        print("  No items found — try a different date range, bbox, or check EODC STAC availability")
        return

    fetch_results = fetcher.fetch(event, output_dir)
    if not fetch_results:
        print("  Fetch returned no results — nothing to plot")
        return

    print(f"  Fetched {len(fetch_results)} result(s):")
    for result in fetch_results:
        for path in result.files:
            print(f"    {path.relative_to(_REPO_ROOT)}")

    # ── Select the date with the most flood pixels ────────────────────────
    best_result = fetch_results[0]
    best_date = fetch_results[0].date_token or "gfm"
    best_flood_count = 0

    for result in fetch_results:
        ds = fetcher.to_dataset(result)
        dlabel = result.date_token or "gfm"
        print(f"\n  {dlabel}")
        if "flood_fraction" in ds:
            flooded = pixel_stats_classified(ds["flood_fraction"].values)
            if flooded > best_flood_count:
                best_flood_count = flooded
                best_result = result
                best_date = dlabel

    best_ds = fetcher.to_dataset(best_result)
    print(f"\n  Plotting peak-flood date: {best_date} ({best_flood_count:,} flooded px)")

    if "flood_fraction" in best_ds:
        plot_classified(
            best_ds["flood_fraction"],
            title=f"{event.event_id}: GFM flood fraction {best_date} (1 arcmin)",
            output_path=png_path,
        )

    # ── Optional harmonise ────────────────────────────────────────────────
    if harmonise:
        from atlantis.harmoniser import Harmoniser

        harm_dir = output_dir / "harmonised"
        harm_dir.mkdir(parents=True, exist_ok=True)
        h = Harmoniser()
        ds_harm = h.harmonise(best_ds, source_id="gfm", flood_variable="flood_fraction")
        flood_var = "flood_fraction" if "flood_fraction" in ds_harm else list(ds_harm.data_vars)[0]
        tif_path = harm_dir / f"{event.event_id}_{best_date}_gfm_harmonised.tif"
        write_harmonised_raster(ds_harm[flood_var], tif_path)
        print(f"  Harmonised GeoTIFF: {tif_path.relative_to(_REPO_ROOT)}")
        pixel_stats_classified(ds_harm[flood_var].values, name="flood fraction (1 arcmin, uint8 %)")
        plot_classified(
            ds_harm[flood_var],
            title=f"{event.event_id}: GFM harmonised {best_date} (1 arcmin, uint8 %)",
            output_path=harmonised_png_path,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Typer application
# ─────────────────────────────────────────────────────────────────────────────

app = typer.Typer(help="GFM fetch and visualisation demo.")


@app.command()
def arbitrary(
    event_id: str = typer.Option("Valencia_2024", "--event-id", help="Label for output file names."),
    bbox: str = typer.Option(
        "-1.5 38.8 0.5 40.0",
        "--bbox",
        help="Bounding box: 'west south east north' in degrees.",
    ),
    start_date: str = typer.Option("2024-10-29", "--start-date", help="Start date (YYYY-MM-DD)."),
    end_date: str = typer.Option("2024-11-04", "--end-date", help="End date (YYYY-MM-DD)."),
    strategy: str = typer.Option(
        "peak",
        "--strategy",
        help="Date selection strategy: peak (most-flooded date), aggregate (mean), all (every date).",
    ),
    coarsen_factor: int = typer.Option(
        4,
        "--coarsen-factor",
        help="Spatial coarsening factor before reprojection (default 4 → ~80 m intermediate).",
    ),
    harmonise: bool = typer.Option(
        False,
        "--harmonise",
        help="Re-encode output as uint8 percent [0,100] (same grid — no resampling needed for GFM).",
    ),
) -> None:
    """Fetch + visualise GFM for any user-defined bbox and date range."""
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
    output_dir = DATA_DIR / event.event_id / "gfm"
    print(f"  BBox: west={w}, south={s}, east={e}, north={n}")
    print(f"  Dates: {event.start_date} -> {event.end_date}")
    print(f"  Output: {output_dir.relative_to(_REPO_ROOT)}")

    _fetch_and_visualise(
        event,
        output_dir,
        png_path=SCRIPTS_DIR / "gfm_arbitrary_event.png",
        harmonised_png_path=SCRIPTS_DIR / "gfm_arbitrary_harmonised.png",
        strategy=strategy,
        coarsen_factor=coarsen_factor,
        harmonise=harmonise,
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
    strategy: str = typer.Option(
        "peak",
        "--strategy",
        help="Date selection strategy: peak (most-flooded date), aggregate (mean), all (every date).",
    ),
    coarsen_factor: int = typer.Option(
        4,
        "--coarsen-factor",
        help="Spatial coarsening factor before reprojection (default 4 → ~80 m intermediate).",
    ),
    harmonise: bool = typer.Option(
        False,
        "--harmonise",
        help="Re-encode output as uint8 percent [0,100] (same grid — no resampling needed for GFM).",
    ),
) -> None:
    """Fetch + visualise GFM for a KuroSiwo catalogue event."""
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
    output_dir = DATA_DIR / event.event_id / "gfm"
    print(f"  Case:   {event.event_id}")
    print(f"  BBox:   {event.bbox}")
    print(f"  Dates:  {event.start_date} -> {event.end_date}  (+-{days_before}/{days_after} days)")
    print(f"  Output: {output_dir.relative_to(_REPO_ROOT)}")

    _fetch_and_visualise(
        event,
        output_dir,
        png_path=SCRIPTS_DIR / "gfm_kurosiwo_event.png",
        harmonised_png_path=SCRIPTS_DIR / "gfm_kurosiwo_harmonised.png",
        strategy=strategy,
        coarsen_factor=coarsen_factor,
        harmonise=harmonise,
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
    print(f"  Visualisations: {SCRIPTS_DIR.relative_to(_REPO_ROOT)}/gfm_*.png")
    print(f"  Data:           {DATA_DIR.relative_to(_REPO_ROOT)}/")


if __name__ == "__main__":
    app()
