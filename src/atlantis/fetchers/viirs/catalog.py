"""VIIRS JPSS tile catalog builder — NOAA S3 archive → GeoParquet inventory.

Iterates day-by-day through a date range, lists available VIIRS TIF tiles on
the NOAA public S3 bucket, and writes a GeoParquet catalogue with tile
footprint polygons from the packaged AOI grid. The output schema matches what
:func:`atlantis.fetchers.viirs.inventory.load_inventory` expects: ``date``,
``aoi_id``, ``s3_key``, ``geometry``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from loguru import logger

from atlantis.batch.catalog import iter_dates, log_progress, retry_request, write_catalogue
from atlantis.fetchers.viirs.backend import NoaaS3Backend

#: Packaged AOI tile grid (same asset the fetch pipeline uses).
_AOI_GRID_PATH = Path(__file__).resolve().parent / "data" / "viirs_aois.geojson"

#: NOAA public S3 bucket base URL.
_DEFAULT_BASE_URL = "https://noaa-jpss.s3.amazonaws.com"

_REQUEST_TIMEOUT = 120

# Regex to extract the AOI integer ID from NOAA filenames like:
# JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/01/01/VIIRS-Flood-1day-GLB003_v1r0_blend_s...tif
_AOI_RE = re.compile(r"GLB(\d{3})_.*\.tif$", re.IGNORECASE)

# Retry / backoff constants (matches the MODIS catalog builder).
_MAX_RETRIES = 5
_BACKOFF_BASE = 1.0


def _parse_aoi_id(s3_key: str) -> int | None:
    """Extract the AOI integer ID from an S3 key."""
    filename = s3_key.rsplit("/", 1)[-1]
    match = _AOI_RE.search(filename)
    return int(match.group(1)) if match else None


def build_catalog(
    start: str,
    end: str,
    output: str | Path,
    *,
    base_url: str = _DEFAULT_BASE_URL,
    timeout: int = _REQUEST_TIMEOUT,
    storage_options: dict[str, Any] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> Path | None:
    """Build the VIIRS JPSS tile catalog and write it to *output*.

    Args:
        start: Start date ``YYYY-MM-DD`` (inclusive).
        end: End date ``YYYY-MM-DD`` (inclusive).
        output: Output destination — a local path or ``s3://`` URI.
        base_url: NOAA S3 bucket base URL.
        timeout: HTTP request timeout in seconds.
        storage_options: fsspec options for S3 writes.
        on_progress: Optional sink for periodic progress messages (see
            :func:`atlantis.batch.catalog.log_progress`). The CLI passes
            ``atlantis.utils.ui.info`` so progress is visible without
            ``--verbose``; left unset it falls back to loguru.

    Returns:
        Path of the written Parquet file (local), or ``None`` when written to
        an ``s3://`` URI.

    Raises:
        FileNotFoundError: If the packaged AOI grid is missing.
        RuntimeError: If no tiles are found in the requested range.
    """
    if not _AOI_GRID_PATH.exists():
        raise FileNotFoundError(f"VIIRS AOI grid not found at {_AOI_GRID_PATH} (run `atlantis setup`).")

    aoi_gdf = gpd.read_file(_AOI_GRID_PATH).to_crs("EPSG:4326")
    aoi_gdf = aoi_gdf[["AOI_ID", "geometry"]].rename(columns={"AOI_ID": "aoi_id"})
    logger.info("Loaded AOI grid: {} tiles", len(aoi_gdf))

    backend = NoaaS3Backend()
    rows: list[dict[str, Any]] = []

    days = list(iter_dates(start, end))
    for i, day in enumerate(days):
        date_str = day.isoformat()
        event_dt = datetime.combine(day, datetime.min.time())
        listing_loc = backend.get_listing_location(base_url=base_url, event_date=event_dt, data_format="tif")

        try:

            def _list(loc=listing_loc) -> list[str]:
                return backend.get_directory_links(base_url=base_url, location=loc.locator, timeout=timeout)

            entries = retry_request(
                _list,
                max_retries=_MAX_RETRIES,
                backoff_base=_BACKOFF_BASE,
                label=f"NOAA listing for {date_str}",
            )
        except Exception as exc:  # noqa: BLE001 - log and continue past a bad day
            logger.warning("Failed to list {}: {}", date_str, exc)
            log_progress(i, len(days), label="VIIRS catalog", on_progress=on_progress)
            continue

        for key in entries:
            aoi_id = _parse_aoi_id(key)
            if aoi_id is not None:
                rows.append({"date": date_str, "aoi_id": aoi_id, "s3_key": key})
        log_progress(i, len(days), label="VIIRS catalog", on_progress=on_progress)

    if not rows:
        logger.error("No VIIRS tiles found in range {} – {}", start, end)
        raise RuntimeError(f"No VIIRS tiles found in range {start} – {end}")

    catalog_df = pd.DataFrame(rows)
    catalog_gdf = catalog_df.merge(aoi_gdf, on="aoi_id", how="left")
    missing_geom = catalog_gdf["geometry"].isna().sum()
    if missing_geom:
        logger.warning("{} rows have no matching AOI geometry (dropped)", missing_geom)
        catalog_gdf = catalog_gdf.dropna(subset=["geometry"])
    catalog_gdf = gpd.GeoDataFrame(catalog_gdf, geometry="geometry", crs="EPSG:4326")

    logger.info(
        "Catalog: {} tiles across {} dates, {} unique AOIs",
        len(catalog_gdf),
        catalog_gdf["date"].nunique(),
        catalog_gdf["aoi_id"].nunique(),
    )
    return write_catalogue(catalog_gdf, output, storage_options=storage_options)
