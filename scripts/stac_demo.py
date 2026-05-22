"""Demo: querying the static KuroSiwo STAC catalog.

Shows four query patterns against the self-contained catalog written to
``data/stac/`` by ``python -m atlantis.stac_catalog``:

1. By event   — retrieve all items for a specific ``actid``
2. By geometry — spatial intersection with a WGS84 bounding box
3. By datetime — temporal range filter on ``ks:flood_date``
4. By KuroSiwo properties — predicate on ``ks:pflood``, ``ks:gvalid``, etc.

Run from the project root (venv active):

    python scripts/stac_demo.py
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pystac
from shapely.geometry import box, shape

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CATALOG_PATH = Path("data/stac/catalog.json")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prop(item: pystac.Item, key: str):
    """Shorthand for ``item.properties[key]`` with a safe default."""
    return item.properties.get(key)


def _item_geometry(item: pystac.Item):
    """Return the item footprint as a Shapely geometry."""
    return shape(item.geometry)


def items_for_event(catalog: pystac.Catalog, actid: int) -> list[pystac.Item]:
    """Return all labeled items belonging to a single flood event (``actid``)."""
    event_col_id = f"kurosiwo-labeled-{actid}"
    labeled_col = catalog.get_child("kurosiwo-labeled")
    if labeled_col is None:
        return []
    event_col = labeled_col.get_child(event_col_id)
    if event_col is None:
        return []
    return list(event_col.get_items())


def items_intersecting_bbox(
    items: list[pystac.Item],
    bbox: tuple[float, float, float, float],
) -> Iterator[pystac.Item]:
    """Yield items whose footprint intersects the given WGS84 bbox.

    Parameters
    ----------
    items:
        Items to filter (already scoped to an event, for example).
    bbox:
        ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
    """
    roi = box(*bbox)
    for item in items:
        if _item_geometry(item).intersects(roi):
            yield item


def items_in_date_range(
    items: list[pystac.Item],
    start: datetime,
    end: datetime,
    field: str = "ks:flood_date",
) -> Iterator[pystac.Item]:
    """Yield items whose ``field`` (a date string) falls within [start, end].

    ``field`` defaults to ``ks:flood_date``.  Pass ``start_datetime`` or
    ``end_datetime`` to filter on the SAR acquisition window instead.
    """
    for item in items:
        raw = _prop(item, field)
        if raw is None:
            continue
        dt = datetime.fromisoformat(raw.replace(" ", "T")).replace(tzinfo=timezone.utc)
        if start <= dt <= end:
            yield item


def items_by_property(
    items: list[pystac.Item],
    *,
    min_pflood: float | None = None,
    max_pflood: float | None = None,
    gvalid: bool | None = None,
    min_pcovered: float | None = None,
    aoiid: int | None = None,
) -> Iterator[pystac.Item]:
    """Yield items matching KuroSiwo-specific property predicates.

    Parameters
    ----------
    min_pflood / max_pflood:
        Patch-level flood percentage threshold (0–100 %).
    gvalid:
        Only include geometrically valid (``ks:gvalid = True``) tiles.
    min_pcovered:
        Minimum SAR data coverage percentage (0–100 %).
    aoiid:
        Exact AOI area-of-interest ID (integer ≥ 1).
    """
    for item in items:
        p = item.properties
        if min_pflood is not None and (p.get("ks:pflood") or 0) < min_pflood:
            continue
        if max_pflood is not None and (p.get("ks:pflood") or 0) > max_pflood:
            continue
        if gvalid is not None and p.get("ks:gvalid") != gvalid:
            continue
        if min_pcovered is not None and (p.get("ks:pcovered") or 0) < min_pcovered:
            continue
        if aoiid is not None and p.get("ks:aoiid") != aoiid:
            continue
        yield item


def _print_item(item: pystac.Item, indent: str = "  ") -> None:
    _ = item.properties
    flood_date = _prop(item, "ks:flood_date")
    pflood = _prop(item, "ks:pflood")
    pcovered = _prop(item, "ks:pcovered")
    gvalid = _prop(item, "ks:gvalid")
    bbox = item.bbox
    print(
        f"{indent}id          : {item.id}\n"
        f"{indent}flood_date  : {flood_date}\n"
        f"{indent}pflood      : {pflood:.1f} %\n"
        f"{indent}pcovered    : {pcovered:.1f} %\n"
        f"{indent}gvalid      : {gvalid}\n"
        f"{indent}bbox        : [{bbox[0]:.4f}, {bbox[1]:.4f}, {bbox[2]:.4f}, {bbox[3]:.4f}]\n"
        f"{indent}assets      : {list(item.assets.keys())}\n"
    )


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


def main() -> None:
    catalog = pystac.Catalog.from_file(str(CATALOG_PATH))
    print(f"Opened catalog: {catalog.id!r}  ({CATALOG_PATH})\n")

    # ------------------------------------------------------------------
    # 1. Query by event (actid)
    # ------------------------------------------------------------------
    ACTID = 1111002
    print(f"{'─' * 60}")
    print(f"1. Items for event actid={ACTID}")
    print(f"{'─' * 60}")
    event_items = items_for_event(catalog, ACTID)
    print(f"   Total labeled tiles: {len(event_items)}")
    _print_item(event_items[0])

    # ------------------------------------------------------------------
    # 2. Spatial filter — bbox around the Lake Chad / Logone-Chari region
    #    (event 1111002 is the 2020 Logone-Chari floods, Chad/Cameroon)
    # ------------------------------------------------------------------
    ROI_BBOX = (14.70, 12.20, 14.95, 12.50)  # (min_lon, min_lat, max_lon, max_lat)
    print(f"{'─' * 60}")
    print(f"2. Spatial filter — bbox {ROI_BBOX}")
    print(f"{'─' * 60}")
    spatial_items = list(items_intersecting_bbox(event_items, ROI_BBOX))
    print(f"   Items intersecting ROI: {len(spatial_items)}")
    for item in spatial_items[:3]:
        _print_item(item)

    # ------------------------------------------------------------------
    # 3. Temporal filter — flood_date within the 2020 flood season
    # ------------------------------------------------------------------
    T_START = datetime(2020, 8, 1, tzinfo=timezone.utc)
    T_END = datetime(2020, 10, 31, tzinfo=timezone.utc)
    print(f"{'─' * 60}")
    print(f"3. Temporal filter — flood_date in [{T_START.date()}, {T_END.date()}]")
    print(f"{'─' * 60}")
    temporal_items = list(items_in_date_range(event_items, T_START, T_END))
    print(f"   Items in date range: {len(temporal_items)}")
    if temporal_items:
        _print_item(temporal_items[0])

    # ------------------------------------------------------------------
    # 4. Property filter — high-confidence flood tiles
    # ------------------------------------------------------------------
    print(f"{'─' * 60}")
    print("4. Property filter — gvalid=True, pflood≥10 %, pcovered≥50 %")
    print(f"{'─' * 60}")
    quality_items = list(
        items_by_property(
            event_items,
            min_pflood=10.0,
            gvalid=True,
            min_pcovered=50.0,
        )
    )
    print(f"   High-confidence flood tiles: {len(quality_items)}")
    for item in quality_items[:3]:
        _print_item(item)

    # ------------------------------------------------------------------
    # 5. Chained filter — spatial + property
    # ------------------------------------------------------------------
    print(f"{'─' * 60}")
    print("5. Chained — spatial ROI + pflood≥5 % + gvalid=True")
    print(f"{'─' * 60}")
    chained = list(
        items_by_property(
            list(items_intersecting_bbox(event_items, ROI_BBOX)),
            min_pflood=5.0,
            gvalid=True,
        )
    )
    print(f"   Matching tiles: {len(chained)}")
    for item in chained:
        # Show master (flood-time) SAR asset href
        master_assets = [
            (key, asset) for key, asset in item.assets.items() if asset.extra_fields.get("ks:master") is True
        ]
        for key, asset in master_assets:
            print(f"    {key:12s}  →  {asset.href}")
    print()


if __name__ == "__main__":
    main()
