r"""VIIRS fetch and visualisation demo.

Demonstrates two independent workflows:

1. **Arbitrary event** — fetch raw VIIRS for any user-supplied bbox and date
   range (no KuroSiwo catalogue needed).

2. **KuroSiwo event** — fetch raw VIIRS for a random (or specified) event
   from the KuroSiwo catalogue metadata CSV.

Visualisations are saved as PNG files under ``scripts/`` (tracked).
Downloaded GeoTIFFs are written under ``scripts/data/`` (untracked, see
``scripts/data/.gitignore``).

.. note::

    The VIIRS fetcher depends on a global AOI tile grid.  If you haven't run
    the repo setup yet, do it once before using this demo::

        uv run python scripts/setup.py

    You can also generate the grid from the showcase notebook::

        notebooks/drafts/kurosiwo_viirs_showcase_cli.ipynb
        (section ``### VIIRS AOI grid bootstrap``)

Usage::

    # Arbitrary bbox + date range — any region on Earth, no catalogue needed
    uv run python scripts/viirs_demo.py --mode arbitrary \\
        --event-id my_flood \\
        --bbox "-1.0 39.0 0.0 40.0" \\
        --start-date 2024-10-29 \\
        --end-date 2024-10-29

    # KuroSiwo event — bbox and dates resolved from the catalogue automatically
    uv run python scripts/viirs_demo.py --mode kurosiwo --ks-case KuroSiwo_470

    # Widen the temporal window around the KuroSiwo flood date
    uv run python scripts/viirs_demo.py --mode kurosiwo --days-before 1 --days-after 1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ── repo root on sys.path so the script works from any cwd ───────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

# ── guard: the VIIRS AOI grid must exist before either demo mode works ───────
_AOI_GRID = _REPO_ROOT / "src" / "atlantis" / "fetchers" / "viirs" / "data" / "viirs_aois.geojson"
if not _AOI_GRID.exists():
    sys.exit(
        "The VIIRS AOI tile grid is missing.\n"
        f"Expected: {_AOI_GRID.relative_to(_REPO_ROOT)}\n\n"
        "Bootstrapping it is a one-time operation — run:\n"
        "  uv run python scripts/setup.py\n\n"
        "You can also generate it from the showcase notebook:\n"
        "  notebooks/drafts/kurosiwo_viirs_showcase_cli.ipynb\n"
        "(section 'VIIRS AOI grid bootstrap')."
    )

from datetime import date  # noqa: E402

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from atlantis.fetchers.viirs import VIIRSFetcher  # noqa: E402
from atlantis.models.event import FloodEvent  # noqa: E402
from atlantis.utils.kurosiwo import (  # noqa: E402
    KUROSIWO_DEFAULT_CATALOGUE,
    KUROSIWO_DEFAULT_METADATA,
    build_kurosiwo_flood_events,
    build_kurosiwo_flood_events_from_catalogue,
)

# ── output directories ────────────────────────────────────────────────────────
SCRIPTS_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPTS_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── VIIRS pixel code legend ───────────────────────────────────────────────────
VIIRS_CODES: dict[int, tuple[str, str]] = {
    1: ("Land", "#8B4513"),
    17: ("Permanent water", "#1f77b4"),
    20: ("Seasonal water", "#17becf"),
    30: ("Cloud", "#cccccc"),
    99: ("Open water", "#4682B4"),
    160: ("Flood (low conf.)", "#FFFF00"),
    170: ("Flood (medium)", "#FFA500"),
    200: ("Flood (high conf.)", "#FF0000"),
}


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────


def _legend_patches() -> list[Patch]:
    return [
        Patch(facecolor=color, edgecolor="black", linewidth=0.5, label=f"{code}: {label}")
        for code, (label, color) in VIIRS_CODES.items()
    ]


def _pixel_stats(data: np.ndarray) -> None:
    vals = data.ravel()
    vals_nonzero = vals[vals > 0]
    if len(vals_nonzero) == 0:
        print("  All pixels are nodata (0).")
        return
    unique, counts = np.unique(vals_nonzero, return_counts=True)
    order = np.argsort(-counts)
    print(f"  Non-zero pixels: {len(vals_nonzero):,} / {len(vals):,} ({100 * len(vals_nonzero) / len(vals):.1f}%)")
    print("  Top pixel codes:")
    for i in order[:8]:
        pct = 100 * counts[i] / len(vals_nonzero)
        label = VIIRS_CODES.get(int(unique[i]), ("unknown",))[0]
        print(f"    {int(unique[i]):3d}  ({label}): {counts[i]:6,} px  ({pct:.1f}%)")
    flood_px = int(vals_nonzero[vals_nonzero >= 160].sum())
    print(f"  Flood pixels (≥160): {flood_px:,}")


def _plot_and_save(raw_da, event_id: str, title: str, output_path: Path) -> None:
    """Render a raw VIIRS raster with a pixel-code legend and save as PNG."""
    fig, (ax, ax_leg) = plt.subplots(
        1,
        2,
        figsize=(14, 7),
        gridspec_kw={"width_ratios": [3, 1]},
        constrained_layout=True,
    )

    raw_da.plot(ax=ax, cmap="turbo", add_colorbar=True, cbar_kwargs={"label": "Pixel code", "shrink": 0.8})
    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.set_xlabel("Longitude (°)")
    ax.set_ylabel("Latitude (°)")

    ax_leg.legend(
        handles=_legend_patches(),
        loc="center",
        fontsize=9,
        title="VIIRS pixel codes",
        title_fontsize=10,
        frameon=True,
        edgecolor="gray",
    )
    ax_leg.axis("off")

    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {output_path.relative_to(_REPO_ROOT)}")


# ─────────────────────────────────────────────────────────────────────────────
# ── Mode: arbitrary ─────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────


def demo_arbitrary_event(
    event_id: str,
    bbox: tuple[float, float, float, float],
    start: date,
    end: date,
) -> None:
    """Fetch VIIRS for a fully user-defined region and date range."""
    print("\n" + "=" * 60)
    print("Arbitrary event — user-defined bbox + date range")
    print("=" * 60)

    event = FloodEvent(
        event_id=event_id,
        bbox=bbox,
        start_date=start,
        end_date=end,
    )
    output_dir = DATA_DIR / event.event_id / "viirs"
    print(f"  Event ID: {event.event_id}")
    print(f"  BBox:     west={bbox[0]}, south={bbox[1]}, east={bbox[2]}, north={bbox[3]}")
    print(f"  Dates:    {event.start_date} → {event.end_date}")
    print(f"  Output:   {output_dir.relative_to(_REPO_ROOT)}")

    fetcher = VIIRSFetcher()

    # Search first so we can report what's available
    search_results = fetcher.search(event)
    print(f"  Found {len(search_results)} VIIRS tile(s) in the NOAA S3 archive")
    if not search_results:
        print("  No tiles found — skipping fetch (try a different date or bbox)")
        return

    fetch_results = fetcher.fetch(event, output_dir)
    if not fetch_results:
        print("  Fetch returned no results — nothing to plot")
        return

    print(f"  Fetched {sum(len(r.files) for r in fetch_results)} file(s):")
    for result in fetch_results:
        for path in result.files:
            print(f"    {path.relative_to(_REPO_ROOT)}")

    # Load and visualise the first result
    ds = fetcher.to_dataset(fetch_results[0])
    raw_da = ds["raw"]
    print(f"  Shape: {raw_da.shape}  dtype: {raw_da.dtype}  range: [{int(raw_da.min())}, {int(raw_da.max())}]")
    _pixel_stats(raw_da.values)

    _plot_and_save(
        raw_da,
        event_id=event.event_id,
        title=f"{event.event_id}: VIIRS raw composite {event.start_date} (375 m)",
        output_path=SCRIPTS_DIR / "viirs_arbitrary_event.png",
    )


# ─────────────────────────────────────────────────────────────────────────────
# ── Mode: kurosiwo ───────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_ks_events(
    ks_case: str,
    days_before: int,
    days_after: int,
) -> list[FloodEvent]:
    """Return FloodEvent(s) for the requested KuroSiwo case."""
    metadata_path = _REPO_ROOT / KUROSIWO_DEFAULT_METADATA
    catalogue_path = _REPO_ROOT / KUROSIWO_DEFAULT_CATALOGUE

    if metadata_path.exists():
        print(f"  Using prebuilt metadata: {metadata_path.relative_to(_REPO_ROOT)}")
        return build_kurosiwo_flood_events(
            metadata_path,
            case=ks_case,
            days_before=days_before,
            days_after=days_after,
        )

    print(f"  Metadata CSV not found — deriving from catalogue: {catalogue_path.relative_to(_REPO_ROOT)}")
    print("  (Run `uv run atlantis build-kurosiwo-metadata` to cache it)")
    return build_kurosiwo_flood_events_from_catalogue(
        catalogue_path,
        case=ks_case,
        days_before=days_before,
        days_after=days_after,
    )


def _pick_random_ks_case() -> str:
    """Return a random KuroSiwo flood_case name from the metadata or catalogue."""
    metadata_path = _REPO_ROOT / KUROSIWO_DEFAULT_METADATA
    catalogue_path = _REPO_ROOT / KUROSIWO_DEFAULT_CATALOGUE

    if metadata_path.exists():
        df = pd.read_csv(metadata_path)
        case = df["flood_case"].dropna().sample(1, random_state=42).iloc[0]
        print(f"  Randomly selected case from metadata CSV: {case}")
        return str(case)

    import geopandas as gpd

    catalogue = gpd.read_file(catalogue_path)
    actid = int(catalogue["actid"].dropna().drop_duplicates().sample(1, random_state=42).iloc[0])
    case = f"KuroSiwo_{actid:03d}"
    print(f"  Randomly selected case from catalogue: {case}")
    return case


def demo_kurosiwo_event(ks_case: str | None, days_before: int, days_after: int) -> None:
    """Fetch VIIRS for a KuroSiwo event (random or specified)."""
    print("\n" + "=" * 60)
    print("KuroSiwo event")
    print("=" * 60)

    if ks_case is None:
        ks_case = _pick_random_ks_case()
    else:
        print(f"  Using specified case: {ks_case}")

    events = _resolve_ks_events(ks_case, days_before, days_after)
    if not events:
        print(f"  No events found for case '{ks_case}' — skipping")
        return

    event = events[0]
    output_dir = DATA_DIR / event.event_id / "viirs"
    print(f"  Event:    {event.event_id}")
    print(f"  BBox:     {event.bbox}")
    print(f"  Dates:    {event.start_date} → {event.end_date}  (±{days_before}/{days_after} days)")
    print(f"  Output:   {output_dir.relative_to(_REPO_ROOT)}")

    fetcher = VIIRSFetcher()

    search_results = fetcher.search(event)
    print(f"  Found {len(search_results)} VIIRS tile(s) in the NOAA S3 archive")
    if not search_results:
        print("  No tiles found — try increasing --days-before / --days-after")
        return

    fetch_results = fetcher.fetch(event, output_dir)
    if not fetch_results:
        print("  Fetch returned no results — nothing to plot")
        return

    print(f"  Fetched {sum(len(r.files) for r in fetch_results)} file(s):")
    for result in fetch_results:
        for path in result.files:
            print(f"    {path.relative_to(_REPO_ROOT)}")

    ds = fetcher.to_dataset(fetch_results[0])
    raw_da = ds["raw"]
    print(f"  Shape: {raw_da.shape}  dtype: {raw_da.dtype}  range: [{int(raw_da.min())}, {int(raw_da.max())}]")
    _pixel_stats(raw_da.values)

    date_label = event.start_date.isoformat()
    _plot_and_save(
        raw_da,
        event_id=event.event_id,
        title=f"{event.event_id}: VIIRS raw composite {date_label} (375 m)",
        output_path=SCRIPTS_DIR / "viirs_kurosiwo_event.png",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    parts = value.split()
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--bbox must be four numbers: west south east north")
    w, s, e, n = (float(p) for p in parts)
    return (w, s, e, n)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument(
        "--mode",
        choices=["arbitrary", "kurosiwo"],
        required=True,
        help="'arbitrary': fetch any user-defined region (requires --bbox/--start-date/--end-date). "
        "'kurosiwo': fetch a KuroSiwo catalogue event (bbox and dates resolved automatically).",
    )

    # ── Arbitrary mode options ────────────────────────────────────────────────
    arb = parser.add_argument_group("Arbitrary mode options (--mode arbitrary)")
    arb.add_argument(
        "--event-id",
        default="my_flood",
        metavar="ID",
        help="Label for output file names (default: my_flood)",
    )
    arb.add_argument(
        "--bbox",
        default="-1.0 39.0 0.0 40.0",
        metavar="'W S E N'",
        help="Bounding box: west south east north in degrees. "
        "Default: '-1.0 39.0 0.0 40.0' (Valencia, Spain — used as a demo fallback).",
    )
    arb.add_argument(
        "--start-date",
        default="2024-10-29",
        metavar="YYYY-MM-DD",
        help="Start date (default: 2024-10-29)",
    )
    arb.add_argument(
        "--end-date",
        default="2024-10-29",
        metavar="YYYY-MM-DD",
        help="End date (default: 2024-10-29)",
    )

    # ── KuroSiwo mode options ─────────────────────────────────────────────────
    ks = parser.add_argument_group("KuroSiwo mode options (--mode kurosiwo)")
    ks.add_argument(
        "--ks-case",
        default=None,
        metavar="CASE",
        help="KuroSiwo flood_case to fetch (e.g. KuroSiwo_470). Omit to pick a random case from the metadata CSV.",
    )
    ks.add_argument(
        "--days-before", type=int, default=0, help="Days before the KuroSiwo flood date to include (default: 0)"
    )
    ks.add_argument(
        "--days-after", type=int, default=0, help="Days after the KuroSiwo flood date to include (default: 0)"
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.mode == "arbitrary":
        demo_arbitrary_event(
            event_id=args.event_id,
            bbox=_parse_bbox(args.bbox),
            start=date.fromisoformat(args.start_date),
            end=date.fromisoformat(args.end_date),
        )
    else:
        demo_kurosiwo_event(
            ks_case=args.ks_case,
            days_before=args.days_before,
            days_after=args.days_after,
        )

    print("\nDone.")
    print(f"  Visualisations: {SCRIPTS_DIR.relative_to(_REPO_ROOT)}/viirs_*.png")
    print(f"  Data:           {DATA_DIR.relative_to(_REPO_ROOT)}/")
