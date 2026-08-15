"""Per-tile processing function for MODIS MCDWD cube batch runs.

``harmonise_modis_granule_payload`` is a top-level, picklable function — no
closures, no lambdas — so Dask can ship it to worker processes without issue.

Pipeline per tile:

  1. **Download** the raw tile from NASA (LAADS HDF4 or LANCE GeoTIFF) into
     a tempfile (~10–20 MB). The tempfile is deleted in a ``finally`` block
     so peak disk = workers × 20 MB ≈ 120 MB total.
  2. **Read** the MCDWD composite codes — for HDF4 the F2 subdataset is
     extracted with ``open_hdf4_tile``; for LANCE the GeoTIFF is opened
     directly. The result is a uint8 raster of 0/1/2/3/255 codes.
  3. **Derive** ``water_fraction``, ``recurring_flood`` and ``reference_water``
     via the MODIS layer registry (single declarative source of truth).
  4. Build a Dataset with the three derived layers, each carrying the
     MODIS tile's pixel-centre coordinates + EPSG:4326 CRS.
  5. **Harmonise** via :class:`Harmoniser` — reprojects every variable to the
     canonical 1-arcmin global grid and synthesises ``exclusion_mask``.
  6. Encode each layer to uint8 (water_fraction [0,100] / 255 nodata;
     masks 0/1; passthrough uint8 layers).
  7. Return the harmonised layers + global-grid coordinates so a coordinator
     can region-write them into the consolidated Zarr datacube.
  8. Drop intermediates and call ``gc.collect()`` so worker RSS stays flat.
"""

from __future__ import annotations

import gc
import os
import tempfile
from pathlib import Path

import numpy as np
import rasterio
import requests
from loguru import logger
from rasterio.transform import from_bounds

from atlantis.archive.writer import _encode_uint8
from atlantis.config import HarmoniseConfig, get_config
from atlantis.fetchers.modis.backend import earthdata_auth_headers
from atlantis.fetchers.modis.layers import SELECTED_COMPOSITE, registry
from atlantis.fetchers.modis.processor import (
    MODIS_TILE_PIXELS,
    open_hdf4_tile,
    tile_bounds_from_hv,
)
from atlantis.harmoniser import Harmoniser
from atlantis.layers import DerivationContext
from atlantis.utils.io import DownloadContentError, _EarthdataSession, _looks_like_html

#: Allow GDAL to use multiple internal threads for warping + reprojection.
#: Independent of the Python GIL so it does not conflict with Dask's
#: "1 Python thread per worker" model.
os.environ.setdefault("GDAL_NUM_THREADS", "2")

#: HTTP chunk size for streaming downloads.
_DOWNLOAD_CHUNK_SIZE = 1 << 20  # 1 MiB
#: HTTP request timeout (seconds) for tile downloads.
_DOWNLOAD_TIMEOUT = 120


def _download_failure_hint(url: str, detail: str) -> str:
    """Actionable message for auth/EULA download failures."""
    return (
        f"LAADS download failed for {url}: {detail}. This usually means the "
        "EARTHDATA_TOKEN is not a valid LAADS application token — create one at "
        "https://ladsweb.modaps.eosdis.nasa.gov/profiles/#app-tokens — or the "
        "LAADS DAAC data archive license has not been accepted: open the URL in "
        "a browser logged into Earthdata and accept the license prompt."
    )


def _reject_html_response(resp: requests.Response, url: str) -> None:
    """Raise :class:`DownloadContentError` when *resp* is an auth/EULA page."""
    content_type = resp.headers.get("Content-Type", "").lower()
    if "text/html" in content_type or "application/xhtml" in content_type:
        raise DownloadContentError(
            _download_failure_hint(url, f"the server returned an HTML page (Content-Type: {content_type or 'unknown'})")
        )


def probe_download(url: str, timeout: int = 60) -> None:
    """Verify *url* is downloadable with the configured Earthdata token.

    Streams only the first chunk and aborts, so a misconfigured token (wrong
    token type, unaccepted archive license) fails fast with an actionable
    error instead of failing every tile inside Dask.

    Raises:
        DownloadContentError: If the server answered with an HTML page or an
            empty body (auth/EULA redirect) instead of binary data.
    """
    headers = earthdata_auth_headers()
    with _EarthdataSession() as session:
        with session.get(url, stream=True, headers=headers, timeout=timeout) as resp:
            resp.raise_for_status()
            _reject_html_response(resp, url)
            chunk = b""
            for chunk_ in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK_SIZE):
                if chunk_:
                    chunk = chunk_
                    break
            if not chunk:
                raise DownloadContentError(_download_failure_hint(url, "the server returned an empty body"))
            if _looks_like_html(chunk):
                raise DownloadContentError(_download_failure_hint(url, "the server returned an HTML page"))


def _download_to_tempfile(url: str, suffix: str) -> Path:
    """Stream *url* (with Earthdata auth) to a tempfile and return its path.

    The caller is responsible for deleting the file when done.

    Raises:
        DownloadContentError: If the server answered with an HTML page
            (auth/EULA redirect) or an empty body instead of binary data.
    """
    headers = earthdata_auth_headers()
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="modis_src_")
    try:
        with os.fdopen(fd, "wb") as fp:
            with _EarthdataSession() as session:
                with session.get(url, stream=True, headers=headers, timeout=_DOWNLOAD_TIMEOUT) as resp:
                    resp.raise_for_status()
                    _reject_html_response(resp, url)
                    first_chunk = True
                    for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK_SIZE):
                        if not chunk:
                            continue
                        if first_chunk and _looks_like_html(chunk):
                            raise DownloadContentError(_download_failure_hint(url, "the server returned an HTML page"))
                        first_chunk = False
                        fp.write(chunk)
            if fp.tell() == 0:
                raise DownloadContentError(_download_failure_hint(url, "the server returned an empty body"))
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    return Path(tmp_path)


def _tile_dataarray(values: np.ndarray, h: int, v: int, name: str):
    """Wrap a native-resolution MODIS tile array into a georeferenced DataArray.

    Uses the MODIS sinusoidal tile grid to synthesise an EPSG:4326 affine from
    the ``(h, v)`` index. Pixel centres are at ``(k + 0.5) * 1/480°`` from the
    western/southern edge so the harmoniser sees a fully-georeferenced array.
    """
    import rioxarray  # noqa: F401 — registers .rio on xarray objects
    import xarray as xr

    west, south, east, north = tile_bounds_from_hv(h, v)
    transform = from_bounds(west, south, east, north, MODIS_TILE_PIXELS, MODIS_TILE_PIXELS)
    height, width = values.shape
    x_coords = transform.c + (np.arange(width) + 0.5) * transform.a
    y_coords = transform.f + (np.arange(height) + 0.5) * transform.e
    da = xr.DataArray(
        values,
        dims=("y", "x"),
        coords={"y": y_coords, "x": x_coords},
        name=name,
    )
    da.rio.write_crs("EPSG:4326", inplace=True)
    da.rio.write_transform(transform, inplace=True)
    return da


def _read_raw_codes(src_path: Path, composite: str, h: int, v: int) -> np.ndarray:
    """Read the MCDWD composite codes (0/1/2/3/255) from a downloaded tile.

    Picks the HDF4 subdataset extraction path for ``.hdf`` inputs and a direct
    rasterio read for everything else (LANCE GeoTIFFs).
    """
    if src_path.suffix.lower() == ".hdf":
        ds = open_hdf4_tile(src_path, composite)
        try:
            data = ds.read(1)
        finally:
            ds.close()
        return data

    with rasterio.open(src_path) as src:
        data = src.read(1)
    return data


def harmonise_modis_granule_payload(task: dict) -> dict:
    """Download + derive + harmonise a single MODIS tile, returning the result in-memory.

    The picklable produce function for the MODIS cube batch. Mirrors
    :func:`atlantis.fetchers.viirs.batch_processor.harmonise_granule_payload`:
    Dask workers run this in parallel, the coordinator streams each payload
    into the consolidated Zarr datacube.

    Args:
        task: Task dict (``task_id``, ``source_uri``, ``date``, ``h``, ``v``)
            as produced by :func:`atlantis.fetchers.modis.inventory.to_tasks`.

    Returns:
        Dict with ``task_id``, ``date``, ``h``, ``v``, the harmonised
        ``water_fraction`` (uint8 [0,100] / 255 nodata), ``exclusion_mask``
        (uint8 0/1), ``reference_water`` (uint8 0/1), ``recurring_flood``
        (uint8 0/1) on the canonical 1-arcmin grid, plus ``y`` / ``x`` global
        pixel centres for the harmonised AOI window.

    Raises:
        RuntimeError: If ``EARTHDATA_TOKEN`` is not set.
        DownloadContentError: If the server answered with an auth/EULA HTML
            page or an empty body instead of binary data.
        FileNotFoundError: If the downloaded file is unreadable.
        Exception: Any download / rasterio / GDAL / harmoniser error
            propagates to Dask so it counts against the retry budget.
    """
    import xarray as xr

    task_id: str = task["task_id"]
    source_uri: str = task["source_uri"]
    date_str: str = task["date"]
    h: int = int(task["h"])
    v: int = int(task["v"])

    cfg = get_config().fetcher
    composite = cfg.modis_composite.upper()
    is_hdf4 = source_uri.lower().endswith(".hdf")
    suffix = ".hdf" if is_hdf4 else ".tif"

    src_path: Path | None = None
    try:
        src_path = _download_to_tempfile(source_uri, suffix=suffix)

        raw_codes = _read_raw_codes(src_path, composite, h, v)

        ctx = DerivationContext(arrays={SELECTED_COMPOSITE: raw_codes})
        water_float = registry.get_derived("water_fraction").derive(ctx)
        reference_water = registry.get_derived("reference_water").derive(ctx)
        recurring_flood = registry.get_derived("recurring_flood").derive(ctx)
        del ctx, raw_codes

        ds = xr.Dataset(
            {
                "water_fraction": _tile_dataarray(water_float, h, v, "water_fraction"),
                "reference_water": _tile_dataarray(reference_water, h, v, "reference_water"),
                "recurring_flood": _tile_dataarray(recurring_flood, h, v, "recurring_flood"),
            }
        )
        del water_float, reference_water, recurring_flood

        harmoniser = Harmoniser(HarmoniseConfig())
        ds_harm = harmoniser.harmonise(ds, source_id="modis", flood_variable="water_fraction")
        del ds

        harm_da = ds_harm["water_fraction"]
        water_u8 = _encode_uint8(harm_da.values)
        exclusion_u8 = ds_harm["exclusion_mask"].values.astype(np.uint8)
        reference_u8 = ds_harm["reference_water"].values.astype(np.uint8)
        recurring_u8 = ds_harm["recurring_flood"].values.astype(np.uint8)
        y = np.asarray(harm_da["y"].values, dtype="float64")
        x = np.asarray(harm_da["x"].values, dtype="float64")

        logger.debug("modis tile {} h{:02d}v{:02d} → shape {}×{}", task_id, h, v, *harm_da.shape)
        return {
            "task_id": task_id,
            "date": date_str,
            "h": h,
            "v": v,
            "water_fraction": water_u8,
            "exclusion_mask": exclusion_u8,
            "reference_water": reference_u8,
            "recurring_flood": recurring_u8,
            "y": y,
            "x": x,
        }
    finally:
        if src_path is not None:
            src_path.unlink(missing_ok=True)
        gc.collect()
