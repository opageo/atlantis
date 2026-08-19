"""Estimate GFM tasks for every KuroSiwo event.

Offline by default: reads ``data/metadata/kurosiwo_metadata_v1.csv`` and
writes an AOI table to ``data/metadata/kurosiwo_aois.csv`` with one row per
event: the event bbox and its date window.

The GFM processing unit is the EQUI7 tile — GFM's native storage unit (one
STAC item per (tile, date), streamed as a whole COG) — so no arcmin block
grid is cut here; the bbox only selects which tiles/dates are in scope, and
the build script creates one task per (event, date, EQUI7 tile).

With ``--with-items`` it additionally counts GFM STAC items per (event, date)
via the per-year S3 catalogues (2021–2025), giving an estimate of the total
number of batch tasks (``Σ (date, tile)`` pairs) and the expected item load
before any heavy processing.

Usage::

    PYTHONPATH=src pixi run -e batch python scripts/estimate_kurosiwo_aois.py
    PYTHONPATH=src pixi run -e batch python scripts/estimate_kurosiwo_aois.py --with-items
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from atlantis.fetchers.gfm.event_tasks import (  # noqa: E402
    DEFAULT_AOI_BUFFER_KM,
    DEFAULT_PRE_FLOOD_PAD_DAYS,
    buffer_bbox,
    count_items_per_aoi_date,
    event_date_windows,
)

METADATA_PATH = _REPO_ROOT / "data" / "metadata" / "kurosiwo_metadata_v1.csv"
OUTPUT_PATH = _REPO_ROOT / "data" / "metadata" / "kurosiwo_aois.csv"
POST_FLOOD_PAD_DAYS = 14


def build_aoi_table(
    metadata: pd.DataFrame,
    pad_days: int = POST_FLOOD_PAD_DAYS,
    pre_pad_days: int = DEFAULT_PRE_FLOOD_PAD_DAYS,
    buffer_km: float = DEFAULT_AOI_BUFFER_KM,
) -> pd.DataFrame:
    """Map every KuroSiwo event to its bbox and date window.

    Returns one row per event with the event bbox (used to select which GFM
    tiles/dates are in scope) and the event's date window, anchored on the
    flood-time acquisition (``date_end``) padded on both sides: the
    pre-flood baseline span of ``date_start`` is reference imagery, not
    floods, so it is never included. Every bbox is widened by *buffer_km* on
    all sides: the SAR footprint is not the flood extent.
    """
    rows: list[dict] = []
    for row in metadata.itertuples(index=False):
        west, south, east, north = float(row.lon_min), float(row.lat_min), float(row.lon_max), float(row.lat_max)
        start, end = event_date_windows(row.date_end, row.date_end, pad_days, pre_pad_days)
        rows.append(
            {
                "event_id": row.flood_case,
                "aoi_id": row.flood_case,
                "aoi_west": round(west, 6),
                "aoi_south": round(south, 6),
                "aoi_east": round(east, 6),
                "aoi_north": round(north, 6),
                "date_start": start.isoformat(),
                "date_end": end.isoformat(),
                "n_dates": (end - start).days + 1,
            }
        )
    table = pd.DataFrame(rows)
    if buffer_km > 0:
        buffered = [
            buffer_bbox(r.aoi_west, r.aoi_south, r.aoi_east, r.aoi_north, buffer_km)
            for r in table.itertuples(index=False)
        ]
        table[["aoi_west", "aoi_south", "aoi_east", "aoi_north"]] = buffered
    return table


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pad-days", type=int, default=POST_FLOOD_PAD_DAYS, help="days after date_end to include")
    parser.add_argument(
        "--buffer-km",
        type=float,
        default=DEFAULT_AOI_BUFFER_KM,
        help="km to widen every AOI bbox on all sides (0 disables)",
    )
    parser.add_argument(
        "--with-items", action="store_true", help="estimate GFM items per (event, date) from S3 catalogues"
    )
    args = parser.parse_args()

    metadata = pd.read_csv(METADATA_PATH, parse_dates=["date_start", "date_end"])
    table = build_aoi_table(metadata, pad_days=args.pad_days, buffer_km=args.buffer_km)
    if args.with_items:
        items = count_items_per_aoi_date(table)
        table = table.merge(items, on=["event_id", "aoi_id"], how="left")
    table.to_csv(OUTPUT_PATH, index=False)

    print(f"AOI table: {len(table)} (event) rows for {table['event_id'].nunique()} events → {OUTPUT_PATH}")
    print(f"  total date-window days (Σ dates): {table['n_dates'].sum():,}")
    print(f"  years covered: {sorted(table['date_start'].str[:4].unique())}")
    if "est_items" in table.columns:
        estimated = table["est_items"].notna()
        print(f"  catalogue item estimate available for {estimated.sum()}/{len(table)} events")
        if estimated.any():
            print(
                f"  estimated items: {table.loc[estimated, 'est_items'].sum():,} "
                f"over {table.loc[estimated, 'est_tile_days'].sum():,} (date, tile) tasks"
            )


if __name__ == "__main__":
    main()
