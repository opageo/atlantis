"""MODIS MCDWD catalog builder — LAADS archive → Parquet inventory.

Queries the LAADS archive API (HTML directory listings) to discover available
MCDWD F2 tiles for a date range, builds a Parquet catalog, and writes it to
a local file or an S3 URI.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from loguru import logger

from atlantis.batch.catalog import iter_dates, log_progress, retry_request, write_catalogue
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

    def _get() -> requests.Response:
        response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code != 404:
            response.raise_for_status()
        return response

    resp = retry_request(
        _get,
        max_retries=_MAX_RETRIES,
        backoff_base=_BACKOFF_BASE,
        exceptions=(requests.RequestException,),
        label=f"LAADS listing for {date_str}",
    )
    if resp.status_code == 404:
        return []

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
        tiles.append({
            "date": date_str,
            "h": h,
            "v": v,
            "task_id": task_id,
            "source_uri": source_uri,
        })
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
    on_progress: Callable[[str], None] | None = None,
) -> Path | None:
    """Build the MODIS MCDWD tile catalog and write it to *output*.

    Args:
        start: Start date ``YYYY-MM-DD`` (inclusive).
        end: End date ``YYYY-MM-DD`` (inclusive).
        output: Output destination — a local path or ``s3://`` URI.
        storage_options: fsspec options for S3 writes.
        on_progress: Optional sink for periodic progress messages (see
            :func:`atlantis.batch.catalog.log_progress`). The CLI passes
            ``atlantis.utils.ui.info`` so progress is visible without
            ``--verbose``; left unset it falls back to loguru.

    Returns:
        Path of the written Parquet file (local), or ``None`` when written to
        an ``s3://`` URI.

    Raises:
        RuntimeError: If ``EARTHDATA_TOKEN`` is not set, or if the LAADS
            listing for a day still fails after :data:`_MAX_RETRIES` attempts
            — a partial catalog is never silently written; the whole build
            aborts so a truncated result can't be mistaken for a complete one.
    """
    headers = earthdata_auth_headers()

    days = list(iter_dates(start, end))
    rows: list[dict[str, Any]] = []
    for i, day in enumerate(days):
        day_str = day.isoformat()
        try:
            rows.extend(_list_tiles_for_date(day_str, headers))
        except requests.RequestException as exc:
            raise RuntimeError(
                f"MODIS catalog build aborted: LAADS listing for {day_str} failed after "
                f"{_MAX_RETRIES} attempt(s) ({len(rows)} row(s) collected from {i} prior day(s)): {exc}"
            ) from exc
        log_progress(i, len(days), label="MODIS catalog", on_progress=on_progress)

    if not rows:
        logger.error("No MODIS tiles found in range {} – {}", start, end)
        raise RuntimeError(f"No MODIS tiles found in range {start} – {end}")

    df = pd.DataFrame(rows, columns=["date", "h", "v", "task_id", "source_uri"])
    logger.info("Catalog: {} tiles across {} dates", len(df), df["date"].nunique())

    return write_catalogue(df, output, storage_options=storage_options)
