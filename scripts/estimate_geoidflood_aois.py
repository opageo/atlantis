"""Estimate GFM tasks for every GEOID-Flood event-AoI.

Offline by default: reads ``data/metadata/geoidflood_metadata_v1.csv``
(derived from the HF tile catalogue by
:func:`atlantis.utils.geoidflood.derive_geoidflood_metadata`; if the CSV is
missing it is derived on the fly from ``assets/geoidflood_tile_catalog.parquet``)
and writes an AOI table to ``data/metadata/geoidflood_aois.csv`` with one row
per (event, native AoI): the event-AoI bbox and its date window.

The GFM processing unit is the EQUI7 tile — GFM's native storage unit (one
STAC item per (tile, date), streamed as a whole COG) — so no arcmin block
grid is cut here; the bbox only selects which tiles/dates are in scope, and
the build script creates one task per (event-AoI, date, EQUI7 tile).

With ``--with-items`` it additionally counts GFM STAC items per (AoI, date)
via the per-year S3 catalogues (2021–2025), giving an estimate of the total
number of batch tasks (``Σ (date, tile)`` pairs) and the expected item load
before any heavy processing.

Usage::

    PYTHONPATH=src pixi run -e batch python scripts/estimate_geoidflood_aois.py
    PYTHONPATH=src pixi run -e batch python scripts/estimate_geoidflood_aois.py --with-items
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from atlantis.fetchers.gfm.event_tasks import count_items_per_aoi_date, event_date_windows  # noqa: E402

METADATA_PATH = _REPO_ROOT / "data" / "metadata" / "geoidflood_metadata_v1.csv"
OUTPUT_PATH = _REPO_ROOT / "data" / "metadata" / "geoidflood_aois.csv"
POST_FLOOD_PAD_DAYS = 14


def build_aoi_table(metadata: pd.DataFrame, pad_days: int = POST_FLOOD_PAD_DAYS) -> pd.DataFrame:
    """Map every GEOID-Flood event-AoI to its bbox and date window.

    Returns one row per (event, native AoI) with the event-AoI bbox (used to
    select which GFM tiles/dates are in scope) and the event's date window.
    """
    rows: list[dict] = []
    for row in metadata.itertuples(index=False):
        west, south, east, north = float(row.lon_min), float(row.lat_min), float(row.lon_max), float(row.lat_max)
        start, end = event_date_windows(row.date_start, row.date_end, pad_days)
        rows.append(
            {
                "event_id": row.event_id,
                "aoi_id": row.aoi_id,
                "aoi_west": round(west, 6),
                "aoi_south": round(south, 6),
                "aoi_east": round(east, 6),
                "aoi_north": round(north, 6),
                "date_start": start.isoformat(),
                "date_end": end.isoformat(),
                "n_dates": (end - start).days + 1,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pad-days", type=int, default=POST_FLOOD_PAD_DAYS, help="days after date_end to include")
    parser.add_argument(
        "--with-items", action="store_true", help="estimate GFM items per (AoI, date) from S3 catalogues"
    )
    args = parser.parse_args()

    if not METADATA_PATH.exists():
        from atlantis.utils.geoidflood import (
            GEOIDFLOOD_DEFAULT_CATALOGUE,
            write_geoidflood_metadata_csv,
        )

        print(f"Metadata CSV missing — deriving from {GEOIDFLOOD_DEFAULT_CATALOGUE}…")
        write_geoidflood_metadata_csv(_REPO_ROOT / GEOIDFLOOD_DEFAULT_CATALOGUE, METADATA_PATH)

    metadata = pd.read_csv(METADATA_PATH, parse_dates=["date_start", "date_end"])
    table = build_aoi_table(metadata, pad_days=args.pad_days)
    if args.with_items:
        items = count_items_per_aoi_date(table)
        table = table.merge(items, on=["event_id", "aoi_id"], how="left")
    table.to_csv(OUTPUT_PATH, index=False)

    print(
        f"AOI table: {len(table)} (event, AoI) rows for "
        f"{table['event_id'].nunique()} events / "
        f"{table.groupby(['event_id', 'aoi_id']).ngroups} event-AoIs → {OUTPUT_PATH}"
    )
    print(f"  total date-window days (Σ dates): {table['n_dates'].sum():,}")
    print(f"  years covered: {sorted(table['date_start'].str[:4].unique())}")
    if args.with_items:
        estimated = table["est_items"].notna()
        print(f"  catalogue item estimate available for {estimated.sum()}/{len(table)} (event, AoI) rows")
        if estimated.any():
            print(
                f"  estimated items: {table.loc[estimated, 'est_items'].sum():,} "
                f"over {table.loc[estimated, 'est_tile_days'].sum():,} (date, tile) tasks"
            )


if __name__ == "__main__":
    main()
