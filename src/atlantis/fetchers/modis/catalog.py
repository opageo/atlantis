"""MODIS MCDWD catalog builder — LAADS archive → Parquet inventory.

Queries the LAADS archive API (HTML directory listings) to discover available
MCDWD F2 tiles for a date range, builds a Parquet catalog, and writes it to
a local file or an S3 URI.
"""

from __future__ import annotations

import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from loguru import logger

from atlantis.fetchers.modis.backend import earthdata_auth_headers

_LAADS_BASE_URL = "https://ladsweb.modaps.eosdis.nasa.gov"

# Retry / backoff constants.
_MAX_RETRIES = 5
_BACKOFF_BASE = 1.0
_REQUEST_TIMEOUT = 60


def _laads_shortname_for_year(year: int) -> str:
    """Return the LAADS shortname (``MCDWD_L3`` or ``MCDWD_L3_NRT``) for *year*."""
    return "MCDWD_L3" if year <= 2025 else "MCDWD_L3_NRT"


def _laads_directory_url(date_str: str) -> str:
    """Build the LAADS directory listing URL for a given ISO date ``YYYY-MM-DD``."""
    d = date.fromisoformat(date_str)
    shortname = _laads_shortname_for_year(d.year)
    doy = d.strftime("%j")
    return f"{_LAADS_BASE_URL}/archive/allData/61/{shortname}/{d.year}/{doy}/"


def _list_tiles_for_date(
    date_str: str,
    headers: dict[str, str],
    timeout: int = _REQUEST_TIMEOUT,
) -> list[dict[str, Any]]:
    """Return a list of tile dicts available for *date_str*."""
    url = _laads_directory_url(date_str)
    logger.debug("Listing {} …", url)

    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            break
        except requests.RequestException as exc:
            if attempt == _MAX_RETRIES - 1:
                raise
            wait = _BACKOFF_BASE * (2**attempt)
            logger.warning(
                "LAADS listing failed for {} (attempt {}/{}): {}. Retrying in {:.0f}s …",
                date_str,
                attempt + 1,
                _MAX_RETRIES,
                exc,
                wait,
            )
            time.sleep(wait)

    matches = re.findall(r'href="([^"]*MCDWD_L3[^"]*\.hdf)"', resp.text, flags=re.IGNORECASE)
    seen: set[str] = set()
    tiles: list[dict[str, Any]] = []
    for href in matches:
        filename = href.rsplit("/", 1)[-1]
        if filename in seen:
            continue
        seen.add(filename)
        hv = _parse_hv_from_modis_filename(filename)
        if hv is None:
            continue
        h, v = hv
        task_id = f"modis-{date_str.replace('-', '')}-h{h:02d}v{v:02d}"
        source_uri = url + filename
        tiles.append(
            {
                "date": date_str,
                "h": h,
                "v": v,
                "task_id": task_id,
                "source_uri": source_uri,
            }
        )
    logger.debug("{} → {} tile(s)", date_str, len(tiles))
    return tiles


def _parse_hv_from_modis_filename(filename: str) -> tuple[int, int] | None:
    """Extract ``(h, v)`` from a MODIS filename (e.g. ``…h24v05…``)."""
    match = re.search(r"\.h(\d{2})v(\d{2})\.", filename)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def build_catalog(
    start: str,
    end: str,
    output: str | Path,
    *,
    storage_options: dict[str, Any] | None = None,
) -> Path:
    """Build the MODIS MCDWD tile catalog and write it to *output*.

    Args:
        start: Start date ``YYYY-MM-DD`` (inclusive).
        end: End date ``YYYY-MM-DD`` (inclusive).
        output: Output destination — a local path or ``s3://`` URI.
        storage_options: fsspec options for S3 writes.

    Returns:
        Path of the written Parquet file (local), or ``None`` when written to
        an ``s3://`` URI.

    Raises:
        RuntimeError: If ``EARTHDATA_TOKEN`` is not set.
    """
    headers = earthdata_auth_headers()
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    days = (end_date - start_date).days + 1

    rows: list[dict[str, Any]] = []
    for i in range(days):
        day = (start_date + timedelta(days=i)).isoformat()
        try:
            rows.extend(_list_tiles_for_date(day, headers))
        except requests.RequestException as exc:
            logger.warning("Failed to list {}: {}", day, exc)

    if not rows:
        logger.error("No MODIS tiles found in range {} – {}", start, end)
        raise RuntimeError(f"No MODIS tiles found in range {start} – {end}")

    df = pd.DataFrame(rows, columns=["date", "h", "v", "task_id", "source_uri"])
    logger.info("Catalog: {} tiles across {} dates", len(df), df["date"].nunique())

    output_str = str(output)
    if output_str.startswith("s3://"):
        import s3fs

        fs = s3fs.S3FileSystem(**(storage_options or {}))
        with fs.open(output_str, "wb") as f:
            df.to_parquet(f, engine="pyarrow", index=False)
    else:
        local_path = Path(output_str)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(local_path, engine="pyarrow", index=False)
    logger.info("Wrote catalog → {}", output_str)
    return Path(output_str) if not output_str.startswith("s3://") else None
