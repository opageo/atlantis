"""Shared per-tile task building for event-based GFM archive builds.

Both ``scripts/build_geoidflood_gfm_archive.py`` and
``scripts/build_kurosiwo_gfm_archive.py`` build one batch task per
(event-AoI, date, EQUI7 tile) — the tile being GFM's native storage unit
(one STAC item per (tile, date), streamed as a whole COG from EODC). Each
task carries one tile's items for one date with the tile's own bbox, so
every task reads exactly the COGs it needs and writes one per-tile cell into
the cube, exactly like the year cubes. Events only select which tiles/dates
are in scope.

This module holds the common task-building logic (and the AOI-table
helpers shared with the estimate scripts) so the two script pairs stay in
sync — previously they duplicated each other and drifted (e.g. the KuroSiwo
build once had a duplicate-task-id bug the GEOID-Flood build had already
fixed).
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta

import pandas as pd

from atlantis.fetchers.gfm.backend import GfmStacBackend
from atlantis.models.event import FloodEvent

#: Years with a published GFM catalogue on S3 (offline task building).
CATALOGUE_YEARS = {"2021", "2022", "2023", "2024", "2025"}

#: Default post-flood pad for event date windows.
DEFAULT_POST_FLOOD_PAD_DAYS = 14

#: Default pre-flood pad for event date windows.
DEFAULT_PRE_FLOOD_PAD_DAYS = 14

#: Default AOI bbox buffer (km) — flood extents spill past the mapped AOI
#: footprint, so bboxes are widened before tile selection. ~11 arcmin ≈ 20 km.
DEFAULT_AOI_BUFFER_KM = 25.0

#: Charset whitelist for EQUI7 tile ids (e.g. ``AF020M_E030N066T3``) —
#: remote STAC ``Equi7Tile`` values are validated against this before they
#: enter task ids / tracker keys.
_TILE_CHARSET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def catalogue_uri(year: str) -> str:
    """S3 URI of the published GFM catalogue for *year*."""
    return f"s3://atlantis/assets/gfm/gfm_archive_catalog_{year}.parquet"


def is_valid_tile(value: object) -> bool:
    """Whether *value* looks like an EQUI7 tile id (type + charset + length)."""
    return isinstance(value, str) and 4 <= len(value) <= 32 and value.isascii() and set(value) <= _TILE_CHARSET


def is_valid_bbox(bbox: object) -> bool:
    """Whether an item bbox is finite, ordered, and within lon/lat range."""
    try:
        west, south, east, north = (float(v) for v in bbox)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return (
        all(math.isfinite(v) for v in (west, south, east, north))
        and west <= east
        and south <= north
        and -180.0 <= west <= east <= 180.0
        and -90.0 <= south <= north <= 90.0
    )


def task_id(event_id: str, aoi_id: str, day: str, tile: str) -> str:
    """Unique task id embedding event, native AoI, EQUI7 tile, and date."""
    return f"gfm-{event_id}-{aoi_id}-{tile}-{day.replace('-', '')}"


def kurosiwo_task_id(event_id: str, aoi_id: str, day: str, tile: str) -> str:
    """Unique KuroSiwo task id embedding event, EQUI7 tile, and date."""
    return f"gfm-{aoi_id}-{tile}-{day.replace('-', '')}"


def event_date_windows(
    start,
    end,
    pad_days: int = DEFAULT_POST_FLOOD_PAD_DAYS,
    pre_pad_days: int = DEFAULT_PRE_FLOOD_PAD_DAYS,
) -> tuple[date, date]:
    """Return the event date window (metadata range + pre/post pads)."""
    return (
        pd.Timestamp(start).date() - timedelta(days=pre_pad_days),
        pd.Timestamp(end).date() + timedelta(days=pad_days),
    )


def buffer_bbox(west: float, south: float, east: float, north: float, km: float) -> list[float]:
    """Widen a bbox by *km* on all sides, returned as ``[west, south, east, north]``.

    One degree of latitude ≈ 111 km; longitude degrees are scaled by
    ``cos(mid latitude)``. Results are clamped to the valid lon/lat range.
    """
    dlat = km / 111.0
    # ponytail: cos floor 0.1 keeps the lon buffer sane near the poles
    cos_lat = max(abs(math.cos(math.radians((south + north) / 2))), 0.1)
    dlon = km / (111.0 * cos_lat)
    return [
        round(max(west - dlon, -180.0), 6),
        round(max(south - dlat, -90.0), 6),
        round(min(east + dlon, 180.0), 6),
        round(min(north + dlat, 90.0), 6),
    ]


def tile_bbox(group: pd.DataFrame) -> list[float]:
    """Union of an item group's bboxes — the tile's own extent."""
    return [
        float(group["west"].min()),
        float(group["south"].min()),
        float(group["east"].max()),
        float(group["north"].max()),
    ]


def build_tasks_from_catalogues(
    aoi_table: pd.DataFrame,
    make_task_id,
) -> tuple[list[dict], set[str]]:
    """Build (date, EQUI7-tile) tasks for catalogue-covered years, offline.

    One task per (event-AoI, date, tile): the tile's items for that date,
    with the tile's own bbox — the same per-cell layout as the year cubes
    (:func:`atlantis.fetchers.gfm.inventory.to_tasks`). Every catalogue year
    inside an event's date window contributes tasks, so events straddling a
    year boundary produce tasks for each of their catalogue years (the
    per-year backfill then filters by ``--year``).

    Args:
        aoi_table: AOI table with ``event_id``, ``aoi_id``, ``date_start``,
            ``date_end`` and ``aoi_west/south/east/north`` columns.
        make_task_id: Callable ``(event_id, aoi_id, day, tile) -> task_id``.

    Returns:
        ``(tasks, live_events)`` — the tasks plus the set of event ids whose
        date window includes non-catalogue years and must be searched
        live.
    """
    from atlantis.fetchers.gfm.inventory import load_inventory

    tasks: list[dict] = []
    catalogues: dict[str, pd.DataFrame] = {}

    def catalogue_for(year: str) -> pd.DataFrame:
        if year not in catalogues:
            catalogues[year] = load_inventory(catalogue_uri(year))
        return catalogues[year]

    for row in aoi_table.itertuples(index=False):
        start_year = int(str(row.date_start)[:4])
        end_year = int(str(row.date_end)[:4])
        for year in range(start_year, end_year + 1):
            year_key = str(year)
            if year_key not in CATALOGUE_YEARS:
                continue
            catalogue = catalogue_for(year_key)
            in_window = (catalogue["date"].astype(str) >= row.date_start) & (
                catalogue["date"].astype(str) <= row.date_end
            )
            intersects = (
                (catalogue["west"] < row.aoi_east)
                & (catalogue["east"] > row.aoi_west)
                & (catalogue["south"] < row.aoi_north)
                & (catalogue["north"] > row.aoi_south)
            )
            for (day, tile), group in catalogue[in_window & intersects].groupby(["date", "equi7_tile"]):
                tasks.append(
                    {
                        "task_id": make_task_id(str(row.event_id), str(row.aoi_id), str(day), tile),
                        "date": str(day),
                        "equi7_tile": tile,
                        "item_hrefs": list(group["item_href"]),
                        "bbox": tile_bbox(group),
                        "event_id": row.event_id,
                        "aoi_id": row.aoi_id,
                    }
                )
    live_events = set(aoi_table.loc[~aoi_table["date_start"].str[:4].isin(CATALOGUE_YEARS), "event_id"])
    return tasks, live_events


def build_tasks_live(
    aoi_table: pd.DataFrame,
    events: set[str],
    make_task_id,
) -> tuple[list[dict], list[dict]]:
    """Live STAC search per (event-AoI, date), grouped into per-tile tasks.

    Search results for one day can span several EQUI7 tiles; each tile's
    items become their own task (with the tile's bbox), matching the
    catalogue path and GFM's native per-tile storage unit.

    Items with missing/invalid ``Equi7Tile`` or bbox metadata cannot be
    placed on a tile task; they are returned in *dropped* (with the reason)
    so callers can persist them and reconcile archive coverage after the
    run instead of losing the data silently.

    Args:
        aoi_table: AOI table (see :func:`build_tasks_from_catalogues`).
        events: Event ids to search live (usually the non-catalogue years).
        make_task_id: Callable ``(event_id, aoi_id, day, tile) -> task_id``.

    Returns:
        ``(tasks, dropped)`` — the tasks and the dropped-item summaries
        (``item_id``, ``item_href``, ``reason``).
    """
    backend = GfmStacBackend()
    tasks: list[dict] = []
    dropped: list[dict] = []
    for row in aoi_table[aoi_table["event_id"].isin(events)].itertuples(index=False):
        day = date.fromisoformat(row.date_start)
        while day <= date.fromisoformat(row.date_end):
            # Catalogue-covered days are already built by
            # `build_tasks_from_catalogues`; skipping them here keeps the task
            # ids unique when a live event's window crosses catalogue years.
            if str(day.year) in CATALOGUE_YEARS:
                day += timedelta(days=1)
                continue
            items = backend.search(
                FloodEvent(
                    event_id=row.event_id,
                    bbox=(row.aoi_west, row.aoi_south, row.aoi_east, row.aoi_north),
                    start_date=day,
                    end_date=day,
                )
            )
            if items:
                by_tile: dict[str, list] = defaultdict(list)
                for item in items:
                    tile = item.properties.get("Equi7Tile")
                    if not is_valid_tile(tile):
                        dropped.append(
                            {
                                "item_id": item.id,
                                "item_href": item.self_href,
                                "reason": f"missing/invalid Equi7Tile: {tile!r}",
                            }
                        )
                        continue
                    if not is_valid_bbox(item.bbox):
                        dropped.append(
                            {
                                "item_id": item.id,
                                "item_href": item.self_href,
                                "reason": f"missing/invalid bbox: {item.bbox!r}",
                            }
                        )
                        continue
                    by_tile[tile].append(item)
                for tile, tile_items in by_tile.items():
                    tasks.append(
                        {
                            "task_id": make_task_id(str(row.event_id), str(row.aoi_id), day.isoformat(), tile),
                            "date": day.isoformat(),
                            "equi7_tile": tile,
                            "item_hrefs": [item.self_href for item in tile_items],
                            "bbox": [
                                float(min(i.bbox[0] for i in tile_items)),
                                float(min(i.bbox[1] for i in tile_items)),
                                float(max(i.bbox[2] for i in tile_items)),
                                float(max(i.bbox[3] for i in tile_items)),
                            ],
                            "event_id": row.event_id,
                            "aoi_id": row.aoi_id,
                        }
                    )
            day += timedelta(days=1)
        print(f"  {row.event_id}-{row.aoi_id}: {sum(1 for t in tasks if t['aoi_id'] == row.aoi_id)} tasks")
    return tasks, dropped


def count_items_per_aoi_date(table: pd.DataFrame) -> pd.DataFrame:
    """Count GFM STAC items per (event-AoI, date) from the per-year S3 catalogues.

    Only years with a published catalogue (2021–2025) get estimates; other
    events report NaN (the benchmark measures their real item load anyway).
    Adds an ``est_items`` column (total across the window), an
    ``est_dates_with_data`` column (days with at least one intersecting
    item), and an ``est_tile_days`` column (distinct ``(date, equi7_tile)``
    pairs — the batch task count, since tasks are per tile).
    """
    from atlantis.fetchers.gfm.inventory import load_inventory

    rows: list[dict] = []
    for row in table.itertuples(index=False):
        year = row.date_start[:4]
        try:
            catalogue = load_inventory(catalogue_uri(year))
        except FileNotFoundError:
            rows.append(
                {
                    "event_id": row.event_id,
                    "aoi_id": row.aoi_id,
                    "est_items": None,
                    "est_dates_with_data": None,
                    "est_tile_days": None,
                }
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
        tile_days = hits[["date", "equi7_tile"]].drop_duplicates().shape[0]
        rows.append(
            {
                "event_id": row.event_id,
                "aoi_id": row.aoi_id,
                "est_items": int(len(hits)),
                "est_dates_with_data": int(hits["date"].nunique()),
                "est_tile_days": int(tile_days),
            }
        )
        print(
            f"  {row.event_id}-{row.aoi_id}: {len(hits)} items / {hits['date'].nunique()} dates / "
            f"{tile_days} tile-days ({year} catalogue)"
        )
    return pd.DataFrame(rows)
