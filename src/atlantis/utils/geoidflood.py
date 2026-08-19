"""Helpers for turning GEOID-Flood tile-catalog data into Atlantis metadata and events.

GEOID-Flood is a multi-modal flood-segmentation benchmark built from Copernicus
EMS Rapid Mapping activations (``EMSR151``–``EMSR871``). Its only spatial
metadata is ``tile_catalog.parquet`` on the Hugging Face Hub — one row per
1024×1024 tile, with per-tile WKB geometry in the event's UTM CRS and
``delineation_time_pre`` / ``delineation_time_post`` acquisition times. There
is no per-event AOI geometry file, so event-AoI bounding boxes and date
windows are derived here by grouping tiles.

The natural units of the benchmark are the *event-AoIs* (``EMSR712-10`` =
activation ``EMSR712``, AoI ``10``); they are what this module exposes, in the
same shape as KuroSiwo's per-event metadata so the shared AOI-block
estimation and GFM cube-build scripts work unchanged.
"""

from __future__ import annotations

import shutil
import sys
import urllib.request
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely.wkb

GEOIDFLOOD_DEFAULT_CATALOGUE = Path("assets/geoidflood_tile_catalog.parquet")
GEOIDFLOOD_DEFAULT_METADATA = Path("data/metadata/geoidflood_metadata_v1.csv")

#: Hugging Face dataset repository hosting the GEOID-Flood tile catalogues.
GEOIDFLOOD_HF_REPO = "links-ads/geoid-flood"
GEOIDFLOOD_HF_BASE_URL = "https://huggingface.co/datasets/links-ads/geoid-flood/resolve/main"

#: Catalogues shipped in the dataset: the main tree (train/val/test) and the
#: held-out tree (EMSR857–871). Same schema, disjoint event ids — merged on
#: download.
GEOIDFLOOD_CATALOG_TREES = ("geoid-flood", "geoid-flood-heldout")

#: Metadata columns required by downstream consumers (estimate script, GFM build).
GEOIDFLOOD_REQUIRED_COLUMNS = {
    "event_id",
    "aoi_id",
    "date_start",
    "date_end",
    "date_of_event",
    "lat_min",
    "lat_max",
    "lon_min",
    "lon_max",
}

#: Delineation times later than this year are corrupt metadata (the release
#: contains rows dated 2041) and are dropped when deriving date windows.
MAX_DELINEATION_YEAR = 2027


def is_lfs_pointer(path: Path) -> bool:
    """Check whether a file is a Git LFS pointer instead of real content."""
    try:
        with path.open("r", encoding="utf-8") as file_handle:
            first_line = file_handle.readline()
    except (UnicodeDecodeError, OSError):
        return False
    return first_line.startswith("version https://git-lfs.github.com/spec")


def fetch_geoidflood_catalog(dest: Path, trees: tuple[str, ...] = GEOIDFLOOD_CATALOG_TREES) -> Path:
    """Download both GEOID-Flood tile catalogues from the HF Hub and merge them.

    Args:
        dest: Destination parquet path (created with parents).
        trees: Which dataset trees to fetch (default: main + held-out).

    Returns:
        The merged catalogue path.

    Raises:
        RuntimeError: If every tree download failed; partial merges (at least
            one tree fetched) are written with a warning printed to stderr.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    frames: list[pd.DataFrame] = []
    for tree in trees:
        url = f"{GEOIDFLOOD_HF_BASE_URL}/{tree}/tile_catalog.parquet"
        tmp = dest.with_name(f"{dest.name}.{tree}.part")
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310
                with tmp.open("wb") as out:
                    shutil.copyfileobj(resp, out, 1 << 20)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            print(f"  warning: failed to fetch {tree}/tile_catalog.parquet: {exc}", file=sys.stderr)
            continue
        frames.append(pd.read_parquet(tmp))
        tmp.unlink(missing_ok=True)
    if not frames:
        raise RuntimeError(f"Could not fetch any GEOID-Flood tile catalogue from {GEOIDFLOOD_HF_BASE_URL}")
    merged = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["event_id", "tile_id", "bbox_id"])
    merged.to_parquet(dest, index=False)
    return dest


def load_geoidflood_catalog(catalog_path: Path) -> pd.DataFrame:
    """Load the GEOID-Flood tile catalogue, decoding per-tile WKB geometry.

    Args:
        catalog_path: Path to the merged ``tile_catalog.parquet``.

    Returns:
        DataFrame with one row per 1024×1024 tile. The ``geometry`` column
        holds decoded shapely polygons; ``utm_crs`` gives each tile's CRS
        (tiles of one event share a CRS, but the frame is not homogeneous).

    Raises:
        FileNotFoundError: If the catalogue is missing — download it with
            ``scripts/download_geoidflood_catalog.py`` first.
    """
    if not catalog_path.exists():
        raise FileNotFoundError(
            f"GEOID-Flood tile catalogue not found: {catalog_path}. "
            "Download it with `pixi run download-geoidflood-catalog` "
            "(or `uv run python scripts/download_geoidflood_catalog.py`)."
        )
    if is_lfs_pointer(catalog_path):
        raise RuntimeError(
            f"GEOID-Flood catalogue at {catalog_path} is a Git LFS pointer "
            "(legacy storage). Replace it by running "
            "`pixi run download-geoidflood-catalog`."
        )
    df = pd.read_parquet(catalog_path)
    df["geometry"] = df["geometry"].map(shapely.wkb.loads)
    return df


def derive_geoidflood_metadata(catalog_path: Path) -> pd.DataFrame:
    """Derive per-event-AoI metadata from the tile catalogue.

    One row per ``(event_id, tile_id)`` — the benchmark's native AoI unit
    (``tile_id`` is the ``N`` in ``EMSR712-N``). The AoI bounding box is the
    union of its tiles' geometries (stored in EPSG:4326 in the catalogue); the
    date window is the span of the pre/post Sentinel-1 delineation
    acquisitions.

    Corrupt rows are dropped: missing delineation times, ``post < pre``, or a
    post-event year beyond :data:`MAX_DELINEATION_YEAR` (the release contains
    2041-dated anomalies).

    Args:
        catalog_path: Path to the merged tile catalogue parquet.

    Returns:
        DataFrame with columns ``event_id``, ``aoi_id``, ``date_start``,
        ``date_end``, ``date_of_event``, ``date_of_max_flood_extent``,
        ``lat_min``, ``lat_max``, ``lon_min``, ``lon_max``, ``n_tiles``,
        ``split``, ``countries``, ``macro_areas_set``, ``is_valid``,
        ``invalid_pixel_frac_mean``.
    """
    catalog = load_geoidflood_catalog(catalog_path)

    pre = pd.to_datetime(catalog["delineation_time_pre"], errors="coerce")
    post = pd.to_datetime(catalog["delineation_time_post"], errors="coerce")
    event_time = pd.to_datetime(catalog["event_time"], errors="coerce")
    valid = pre.notna() & post.notna() & (post >= pre) & (post.dt.year <= MAX_DELINEATION_YEAR)
    dropped = int((~valid).sum())
    if dropped:
        print(f"  dropped {dropped} tile row(s) with invalid delineation times")

    catalog = catalog[valid].copy()

    # Tile geometries are stored in EPSG:4326 degrees already — the per-tile
    # ``utm_crs`` column describes the raster grid, not the geometry column
    # (verified against the release: all coordinates fall in [-180, 180] ×
    # [-90, 90] and match each event's known geography).
    catalog["geometry"] = gpd.GeoSeries(catalog["geometry"], crs="EPSG:4326")

    records: list[dict] = []
    for (event_id, aoi), rows in catalog.groupby(["event_id", "tile_id"], sort=False):
        union = gpd.GeoSeries(rows["geometry"]).union_all()
        west, south, east, north = union.bounds
        start = pre.loc[rows.index].min().date()
        end = post.loc[rows.index].max().date()
        evt = event_time.loc[rows.index]
        evt_start = evt.min().date() if evt.notna().any() else start
        records.append(
            {
                "event_id": str(event_id),
                "aoi_id": str(aoi),
                "date_start": start.isoformat(),
                "date_end": end.isoformat(),
                "date_of_event": evt_start.isoformat(),
                "date_of_max_flood_extent": end.isoformat(),
                "lat_min": round(float(south), 4),
                "lat_max": round(float(north), 4),
                "lon_min": round(float(west), 4),
                "lon_max": round(float(east), 4),
                "n_tiles": int(len(rows)),
                "split": rows["split"].iloc[0],
                "countries": rows["countries"].iloc[0],
                "macro_areas_set": rows["macro_areas_set"].iloc[0],
                "is_valid": bool(rows["is_valid"].all()),
                "invalid_pixel_frac_mean": round(float(rows["invalid_pixel_frac"].mean()), 4),
            }
        )
    return pd.DataFrame(records).sort_values(["event_id", "aoi_id"]).reset_index(drop=True)


def write_geoidflood_metadata_csv(catalog_path: Path, output_path: Path) -> Path:
    """Derive and write GEOID-Flood event-AoI metadata as CSV.

    Args:
        catalog_path: Path to the merged tile catalogue parquet.
        output_path: Destination CSV path.

    Returns:
        Path to the written CSV file.
    """
    dataframe = derive_geoidflood_metadata(catalog_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    dataframe.to_csv(output_path, index=False)
    return output_path


def load_geoidflood_metadata(metadata_path: Path) -> pd.DataFrame:
    """Load and validate the GEOID-Flood event-AoI metadata CSV."""
    if not metadata_path.exists():
        raise FileNotFoundError(f"GEOID-Flood metadata CSV not found: {metadata_path}")

    dataframe = pd.read_csv(metadata_path, parse_dates=["date_start", "date_end"])
    missing = GEOIDFLOOD_REQUIRED_COLUMNS - set(dataframe.columns)
    if missing:
        missing_columns = ", ".join(sorted(missing))
        raise ValueError(f"GEOID-Flood metadata is missing required columns: {missing_columns}")

    return dataframe.sort_values(["event_id", "aoi_id"]).reset_index(drop=True)
