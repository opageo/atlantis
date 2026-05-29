#!/usr/bin/env python3
r"""Atlantis setup — bootstrap data assets required by the fetchers.

Run this **once** after cloning the repository (and whenever a new data source
is added to ensure its prerequisites are present).

Currently handles:
* VIIRS — global AOI tile grid (``src/atlantis/fetchers/viirs/data/viirs_aois.geojson``)

Usage::

    uv run python scripts/setup.py

Each step is idempotent — re-running skips assets that already exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


# ─────────────────────────────────────────────────────────────────────────────
# VIIRS AOI grid
# ─────────────────────────────────────────────────────────────────────────────


def _build_viirs_aoi_grid(target: Path) -> None:
    """Generate the global VIIRS AOI tile grid (15° × 15°).

    Based on ``notebooks/ecmwf/Extract_VIIRS_inundation.ipynb``.
    """
    import geopandas as gpd
    from shapely.geometry import box

    try:
        import cartopy.io.shapereader as shpreader  # type: ignore[import-untyped]
    except ImportError:
        print("This step needs cartopy.  Install with:  uv sync --extra notebooks")
        raise

    # Natural Earth 110m land mask
    shpfilename = shpreader.natural_earth(resolution="110m", category="physical", name="land")
    land = gpd.read_file(shpfilename).to_crs("EPSG:4326")
    land_union = land.geometry.union_all()

    aois: list[tuple[int, box]] = []
    aoi_id = 1
    skipped = 0

    # Block 1: Americas + Atlantic (lat 75° → 15°)
    for lat_max, lat_min in zip([75, 60, 45, 30, 15], [60, 45, 30, 15, 0]):  # noqa: B905
        for lon_min, lon_max in zip(
            [-180, -165, -150, -135, -120, -105, -90, -75],  # noqa: B905
            [-165, -150, -135, -120, -105, -90, -75, -60],
        ):
            tile = box(lon_min, lat_min, lon_max, lat_max)
            if not tile.intersects(land_union):
                continue
            if aoi_id == 11 and skipped == 0:
                skipped = 1
                continue
            if aoi_id == 16:
                aoi_id = 17
            if aoi_id == 22 and skipped == 1:
                skipped = 2
                continue
            aois.append((aoi_id, tile))
            aoi_id += 1

    # Block 2: South America + Africa (lat 15° → -60°)
    skipped = 0
    for lat_max, lat_min in zip([15, 0, -15, -30, -45], [0, -15, -30, -45, -60]):  # noqa: B905
        for lon_min, lon_max in zip(
            [-105, -90, -75, -60, -45],  # noqa: B905
            [-90, -75, -60, -45, -30],
        ):
            tile = box(lon_min, lat_min, lon_max, lat_max)
            if not tile.intersects(land_union):
                continue
            if aoi_id == 34 and skipped == 0:
                skipped = 1
                continue
            aois.append((aoi_id, tile))
            aoi_id += 1

    # Block 3: Europe + Northern Asia (lat 75° → 30°)
    skipped = 0
    for lat_max, lat_min in zip([75, 60, 45], [60, 45, 30]):  # noqa: B905
        for lon_min, lon_max in zip(
            [-15, 0, 15, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165],  # noqa: B905
            [0, 15, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165, 180],
        ):
            tile = box(lon_min, lat_min, lon_max, lat_max)
            if not tile.intersects(land_union):
                continue
            if aoi_id == 50:
                aoi_id = 52
            aois.append((aoi_id, tile))
            aoi_id += 1

    # Block 4: Africa + Asia + Oceania (lat 30° → -60°)
    aoi_id = 82
    skipped = 0
    for lat_max, lat_min in zip([30, 15, 0, -15, -30, -45], [15, 0, -15, -30, -45, -60]):  # noqa: B905
        for lon_min, lon_max in zip(
            [-30, -15, 0, 15, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165],  # noqa: B905
            [-15, 0, 15, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165, 180],
        ):
            tile = box(lon_min, lat_min, lon_max, lat_max)
            if not tile.intersects(land_union):
                continue
            if aoi_id == 108:
                aoi_id = 109
            if aoi_id == 113 and skipped == 0:
                skipped = 1
                continue
            if aoi_id == 113 and skipped == 1:
                skipped = 2
                continue
            if aoi_id == 121 and skipped == 2:
                skipped = 3
                continue
            if aoi_id == 128 and skipped == 3:
                skipped = 4
                continue
            aois.append((aoi_id, tile))
            aoi_id += 1

    # Manual additions — tiles that the grid sweep missed
    manual: list[tuple[int, tuple[float, float, float, float]]] = [
        (16, (-60, 45, -45, 60)),
        (50, (90, 75, 105, 90)),
        (51, (105, 75, 120, 90)),
        (81, (150, 30, 165, 45)),
        (108, (75, -15, 90, 0)),
        (129, (-150, 45, -135, 60)),
        (130, (-60, 60, -45, 75)),
        (131, (-45, 60, -30, 75)),
        (132, (-30, 60, -15, 75)),
        (133, (-160, 15, -150, 25)),
        (134, (150, -15, 165, 0)),
        (135, (165, -30, 180, -15)),
        (136, (-180, -20, -165, -5)),
    ]
    for aoi_id_man, bbox in manual:
        aois.append((aoi_id_man, box(*bbox)))

    aois.sort(key=lambda x: x[0])
    gdf_aois = gpd.GeoDataFrame(
        {"AOI_ID": [a[0] for a in aois]},
        geometry=[a[1] for a in aois],
        crs="EPSG:4326",
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    gdf_aois.to_file(target, driver="GeoJSON")
    print(f"  VIIRS AOI grid — {len(gdf_aois)} tiles → {target.relative_to(_REPO_ROOT)}")


# ═════════════════════════════════════════════════════════════════════════════
# Registry of setup steps — add new data sources here
# ═════════════════════════════════════════════════════════════════════════════

STEPS: list[tuple[str, Path, callable]] = [
    (
        "VIIRS AOI grid",
        _REPO_ROOT / "src" / "atlantis" / "fetchers" / "viirs" / "data" / "viirs_aois.geojson",
        _build_viirs_aoi_grid,
    ),
]


def main() -> None:
    print("Atlantis setup\n")

    any_missing = False
    for label, path, builder in STEPS:
        if path.exists():
            print(f"[skip] {label} — already exists: {path.relative_to(_REPO_ROOT)}")
        else:
            any_missing = True
            print(f"[run]  {label} — generating …")
            builder(path)

    if any_missing:
        print("\nSetup complete.")
    else:
        print("\nNothing to do — all data assets are present.")


if __name__ == "__main__":
    main()