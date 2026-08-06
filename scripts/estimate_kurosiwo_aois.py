"""Estimate 512×512-arcmin AOI blocks for every KuroSiwo event.

Offline by default: reads ``data/metadata/kurosiwo_metadata_v1.csv``, maps
each event to the origin-anchored 512×512-arcmin blocks covering its bbox
(:func:`atlantis.archive.grid.aoi_blocks`), and writes the AOI table to
``data/metadata/kurosiwo_aois.csv`` with per-event date windows.

With ``--with-items`` it additionally counts GFM STAC items per (AOI, date)
via live EODC queries (one search per day — a few hundred), giving an
estimate of the total number of batch tasks (``Σ dates × blocks``) and the
expected item load before any heavy processing.

Usage::

    PYTHONPATH=src pixi run -e batch python scripts/estimate_kurosiwo_aois.py
    PYTHONPATH=src pixi run -e batch python scripts/estimate_kurosiwo_aois.py --with-items
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from atlantis.archive.grid import aoi_block_id, aoi_blocks  # noqa: E402

METADATA_PATH = _REPO_ROOT / "data" / "metadata" / "kurosiwo_metadata_v1.csv"
OUTPUT_PATH = _REPO_ROOT / "data" / "metadata" / "kurosiwo_aois.csv"
POST_FLOOD_PAD_DAYS = 14


def event_date_windows(start, end, pad_days: int = POST_FLOOD_PAD_DAYS) -> tuple[date, date]:
    """Return the event date window (metadata range + pad)."""
    return pd.Timestamp(start).date(), pd.Timestamp(end).date() + timedelta(days=pad_days)


def build_aoi_table(metadata: pd.DataFrame, pad_days: int = POST_FLOOD_PAD_DAYS) -> pd.DataFrame:
    """Map every KuroSiwo event to its covering 512×512-arcmin AOI blocks.

    Returns one row per (event, AOI block) with the block bbox (grid edge
    bounds, for STAC queries) and the event's date window.
    """
    rows: list[dict] = []
    for row in metadata.itertuples(index=False):
        west, south, east, north = float(row.lon_min), float(row.lat_min), float(row.lon_max), float(row.lat_max)
        start, end = event_date_windows(row.date_start, row.date_end, pad_days)
        for block in aoi_blocks(west, south, east, north):
            res = 1.0 / 60.0
            rows.append(
                {
                    "event_id": row.flood_case,
                    "aoi_id": aoi_block_id(block),
                    "aoi_west": round(-180.0 + block.col_start * res, 6),
                    "aoi_south": round(90.0 - block.row_stop * res, 6),
                    "aoi_east": round(-180.0 + block.col_stop * res, 6),
                    "aoi_north": round(90.0 - block.row_start * res, 6),
                    "aoi_height": block.height,
                    "aoi_width": block.width,
                    "date_start": start.isoformat(),
                    "date_end": end.isoformat(),
                    "n_dates": (end - start).days + 1,
                }
            )
    return pd.DataFrame(rows)


def count_items_per_aoi_date(table: pd.DataFrame) -> pd.DataFrame:
    """Count GFM STAC items per (AOI, date) from the per-year S3 catalogues.

    Only years with a published catalogue (2021–2025) get estimates; other
    events report NaN (the benchmark measures their real item load anyway).
    Adds an ``est_items`` column (total across the window) and an
    ``est_dates_with_data`` column (days with at least one intersecting item).
    """
    from atlantis.fetchers.gfm.inventory import load_inventory

    rows: list[dict] = []
    for row in table.itertuples(index=False):
        year = row.date_start[:4]
        uri = f"s3://atlantis/assets/gfm/gfm_archive_catalog_{year}.parquet"
        try:
            catalogue = load_inventory(uri)
        except FileNotFoundError:
            rows.append(
                {"event_id": row.event_id, "aoi_id": row.aoi_id, "est_items": None, "est_dates_with_data": None}
            )
            continue
        in_window = (catalogue["date"].astype(str) >= row.date_start) & (catalogue["date"].astype(str) <= row.date_end)
        intersects = (
            (catalogue["west"] < row.aoi_east)
            & (catalogue["east"] > row.aoi_west)
            & (catalogue["south"] < row.aoi_north)
            & (catalogue["north"] > row.aoi_south)
        )
        hits = catalogue[in_window & intersects]
        rows.append(
            {
                "event_id": row.event_id,
                "aoi_id": row.aoi_id,
                "est_items": int(len(hits)),
                "est_dates_with_data": int(hits["date"].nunique()),
            }
        )
        print(f"  {row.event_id} {row.aoi_id}: {len(hits)} items / {hits['date'].nunique()} dates ({year} catalogue)")
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pad-days", type=int, default=POST_FLOOD_PAD_DAYS, help="days after date_end to include")
    args = parser.parse_args()

    metadata = pd.read_csv(METADATA_PATH, parse_dates=["date_start", "date_end"])
    table = build_aoi_table(metadata, pad_days=args.pad_days)
    items = count_items_per_aoi_date(table)
    table = table.merge(items, on=["event_id", "aoi_id"], how="left")
    table.to_csv(OUTPUT_PATH, index=False)

    print(f"AOI table: {len(table)} (event, block) rows for {table['event_id'].nunique()} events → {OUTPUT_PATH}")
    print(f"  blocks per event: {table.groupby('event_id').size().describe().to_dict()}")
    print(f"  total estimated tasks (Σ dates × blocks): {table['n_dates'].sum():,}")
    print(f"  years covered: {sorted(table['date_start'].str[:4].unique())}")
    estimated = table["est_items"].notna()
    print(f"  catalogue item estimate available for {estimated.sum()}/{len(table)} (event, block) rows")
    if estimated.any():
        print(
            f"  estimated items: {table.loc[estimated, 'est_items'].sum():,} "
            f"over {table.loc[estimated, 'est_dates_with_data'].sum():,} (AOI,date) pairs"
        )


if __name__ == "__main__":
    main()
