"""GFM EODC STAC catalog builder — global tile/date inventory → Parquet.

Queries the EODC STAC API (public, no auth required) for every calendar day
in a date range and records one row per returned item: its acquisition date,
EQUI7 tile id (the fixed global grid GFM items are already tiled on — the GFM
analog of MODIS's ``h``/``v``), a STAC self-href (re-fetched at batch time so
the catalogue itself stays small), and the item's WGS84 bbox.

Unlike VIIRS/MODIS, a single ``(date, tile)`` cell can have more than one
matching STAC item (e.g. ascending + descending Sentinel-1 passes on the same
day) — this module only records the raw per-item rows; grouping them into one
accumulating task per cell happens in
:func:`atlantis.fetchers.gfm.inventory.to_tasks`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date as _date
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from atlantis.batch.catalog import iter_dates, log_progress, retry_request, write_catalogue
from atlantis.fetchers.gfm.backend import DEFAULT_GFM_STAC_URL, GFM_COLLECTION_ID

# Retry / backoff constants (mirrors atlantis.fetchers.modis.catalog).
_MAX_RETRIES = 5
_BACKOFF_BASE = 1.0

#: Columns written to the GFM catalogue Parquet file, one row per STAC item.
_CATALOGUE_COLUMNS = ["date", "equi7_tile", "item_id", "item_href", "west", "south", "east", "north"]


def _search_day(api_url: str, day: _date) -> list[dict[str, Any]]:
    """Query the EODC STAC API for every GFM item on *day*, globally (no bbox filter)."""
    from pystac_client import Client

    day_str = day.isoformat()
    start = datetime(day.year, day.month, day.day, 0, 0, 0)
    end = datetime(day.year, day.month, day.day, 23, 59, 59)

    def _search() -> list:
        catalog = Client.open(api_url)
        search = catalog.search(
            collections=GFM_COLLECTION_ID,
            datetime=(start, end),
            max_items=None,
        )
        return list(search.items())

    items = retry_request(
        _search,
        max_retries=_MAX_RETRIES,
        backoff_base=_BACKOFF_BASE,
        exceptions=(Exception,),
        label=f"GFM STAC search for {day_str}",
    )

    rows: list[dict[str, Any]] = []
    for item in items:
        tile = item.properties.get("Equi7Tile")
        if not tile:
            logger.warning("GFM item {} has no Equi7Tile property, skipping", item.id)
            continue
        bbox = item.bbox
        if bbox is None:
            logger.warning("GFM item {} has no bbox, skipping", item.id)
            continue
        rows.append({
            "date": day_str,
            "equi7_tile": tile,
            "item_id": item.id,
            "item_href": item.self_href,
            "west": bbox[0],
            "south": bbox[1],
            "east": bbox[2],
            "north": bbox[3],
        })
    return rows


def build_catalog(
    start: str,
    end: str,
    output: str | Path,
    *,
    api_url: str = DEFAULT_GFM_STAC_URL,
    storage_options: dict[str, Any] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> Path | None:
    """Build the GFM STAC-item catalog and write it to *output*.

    Queries the EODC STAC API once per calendar day in ``[start, end]``
    (inclusive), globally — no bbox filter, matching the VIIRS/MODIS catalog
    CLI signature. Each STAC item becomes one catalogue row; rows sharing a
    ``(date, equi7_tile)`` cell are grouped into a single accumulating batch
    task later, in :func:`atlantis.fetchers.gfm.inventory.to_tasks`.

    Args:
        start: Start date ``YYYY-MM-DD`` (inclusive).
        end: End date ``YYYY-MM-DD`` (inclusive).
        output: Output destination — a local path or ``s3://`` URI.
        api_url: EODC STAC API endpoint. Defaults to the public endpoint.
        storage_options: fsspec options for S3 writes.
        on_progress: Optional sink for periodic progress messages (see
            :func:`atlantis.batch.catalog.log_progress`). The CLI passes
            ``atlantis.utils.ui.info`` so progress is visible without
            ``--verbose``; left unset it falls back to loguru.

    Returns:
        Path of the written Parquet file (local), or ``None`` when written to
        an ``s3://`` URI.

    Raises:
        RuntimeError: If no GFM items were found in the date range, or if the
            STAC search for a day still fails after :data:`_MAX_RETRIES`
            attempts — a partial catalog is never silently written; the whole
            build aborts so a truncated result can't be mistaken for a
            complete one.
    """
    days = list(iter_dates(start, end))
    rows: list[dict[str, Any]] = []
    for i, day in enumerate(days):
        try:
            rows.extend(_search_day(api_url, day))
        except Exception as exc:
            raise RuntimeError(
                f"GFM catalog build aborted: STAC search for {day.isoformat()} failed after "
                f"{_MAX_RETRIES} attempt(s) ({len(rows)} row(s) collected from {i} prior day(s)): {exc}"
            ) from exc
        log_progress(i, len(days), label="GFM catalog", on_progress=on_progress)

    if not rows:
        logger.error("No GFM items found in range {} – {}", start, end)
        raise RuntimeError(f"No GFM items found in range {start} – {end}")

    df = pd.DataFrame(rows, columns=_CATALOGUE_COLUMNS)
    logger.info(
        "GFM catalog: {} items across {} dates, {} tiles",
        len(df),
        df["date"].nunique(),
        df["equi7_tile"].nunique(),
    )

    return write_catalogue(df, output, storage_options=storage_options)
