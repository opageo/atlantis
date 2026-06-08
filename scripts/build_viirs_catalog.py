"""Build a GeoParquet catalog of VIIRS tiles from the NOAA S3 archive.

Iterates day-by-day through a given year, lists available TIF tiles on the
NOAA public S3 bucket, and writes a GeoParquet file with tile footprint
polygons from the packaged AOI grid.

Usage::

    uv run python scripts/build_viirs_catalog.py --year 2020 --output data/viirs_2020_catalog.parquet
"""

from __future__ import annotations

import re
import time
from datetime import date, timedelta
from pathlib import Path

import geopandas as gpd
import pandas as pd
import typer
from loguru import logger

from atlantis.fetchers.viirs.backend import NoaaS3Backend

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://noaa-jpss.s3.amazonaws.com"
AOI_GRID_PATH = _REPO_ROOT / "src" / "atlantis" / "fetchers" / "viirs" / "data" / "viirs_aois.geojson"

# Regex to extract AOI ID from NOAA filenames like:
# JPSS_Blended_Products/VFM_1day_GLB/TIF/2020/01/01/VIIRS-Flood-1day-GLB003_v1r0_blend_s...tif
_AOI_RE = re.compile(r"GLB(\d{3})_.*\.tif$", re.IGNORECASE)

app = typer.Typer(add_completion=False)


def _days_in_year(year: int) -> list[date]:
    """Return all dates in a given year."""
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _parse_aoi_id(s3_key: str) -> int | None:
    """Extract the AOI integer ID from an S3 key."""
    filename = s3_key.rsplit("/", 1)[-1]
    m = _AOI_RE.search(filename)
    return int(m.group(1)) if m else None


@app.command()
def build(
    year: int = typer.Option(2020, help="Year to catalog."),
    output: Path = typer.Option(..., help="Output path for the GeoParquet file."),
    base_url: str = typer.Option(DEFAULT_BASE_URL, help="NOAA S3 bucket base URL."),
    timeout: int = typer.Option(120, help="HTTP request timeout in seconds."),
    delay: float = typer.Option(0.2, help="Delay in seconds between daily requests."),
) -> None:
    """Build a GeoParquet catalog of VIIRS TIF tiles for a given year."""
    if not AOI_GRID_PATH.exists():
        logger.error("AOI grid not found at {}", AOI_GRID_PATH)
        raise typer.Exit(1)

    # Load AOI grid
    aoi_gdf = gpd.read_file(AOI_GRID_PATH).to_crs("EPSG:4326")
    aoi_gdf = aoi_gdf[["AOI_ID", "geometry"]].rename(columns={"AOI_ID": "aoi_id"})
    logger.info("Loaded AOI grid: {} tiles", len(aoi_gdf))

    backend = NoaaS3Backend()
    days = _days_in_year(year)
    logger.info("Cataloging {} days for year {}", len(days), year)

    rows: list[dict] = []
    failed_days = 0

    for i, d in enumerate(days):
        # Build the S3 prefix for this day
        dt = pd.Timestamp(d)  # NoaaS3Backend expects a datetime-like
        listing_loc = backend.get_listing_location(
            base_url=base_url,
            event_date=dt.to_pydatetime(),
            data_format="tif",
        )

        try:
            entries = backend.get_directory_links(
                base_url=base_url,
                location=listing_loc.locator,
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning("Failed to list {}: {}", d.isoformat(), exc)
            failed_days += 1
            time.sleep(delay)
            continue

        for key in entries:
            aoi_id = _parse_aoi_id(key)
            if aoi_id is not None:
                rows.append({"date": d, "aoi_id": aoi_id, "s3_key": key})

        # Progress logging every 30 days
        if (i + 1) % 30 == 0 or (i + 1) == len(days):
            logger.info(
                "Progress: {}/{} days processed, {} tiles found so far",
                i + 1,
                len(days),
                len(rows),
            )

        if delay > 0:
            time.sleep(delay)

    if not rows:
        logger.error("No tiles found for year {}. Exiting.", year)
        raise typer.Exit(1)

    # Build DataFrame and join with AOI geometry
    catalog_df = pd.DataFrame(rows)
    catalog_gdf = catalog_df.merge(aoi_gdf, on="aoi_id", how="left")
    catalog_gdf = gpd.GeoDataFrame(catalog_gdf, geometry="geometry", crs="EPSG:4326")

    # Drop rows with no geometry match (shouldn't happen, but be safe)
    missing_geom = catalog_gdf.geometry.isna().sum()
    if missing_geom > 0:
        logger.warning("{} rows have no matching AOI geometry (dropped)", missing_geom)
        catalog_gdf = catalog_gdf.dropna(subset=["geometry"])

    # Ensure date column is proper date type
    catalog_gdf["date"] = pd.to_datetime(catalog_gdf["date"]).dt.date

    # Write output
    output.parent.mkdir(parents=True, exist_ok=True)
    catalog_gdf.to_parquet(output)

    # Summary
    file_size_mb = output.stat().st_size / (1024 * 1024)
    logger.success(
        "Catalog written to {} ({:.2f} MB)\n  Rows: {:,}\n  Date range: {} to {}\n  Unique AOIs: {}\n  Failed days: {}",
        output,
        file_size_mb,
        len(catalog_gdf),
        catalog_gdf["date"].min(),
        catalog_gdf["date"].max(),
        catalog_gdf["aoi_id"].nunique(),
        failed_days,
    )


if __name__ == "__main__":
    app()
