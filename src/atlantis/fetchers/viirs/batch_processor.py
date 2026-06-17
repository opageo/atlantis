"""Per-granule processing function for VIIRS JPSS batch runs.

``process_granule`` is a top-level, picklable function — no closures, no
lambdas — so Dask can ship it to worker processes without issue.

Pipeline per granule:
  1. **Download** the raw granule from NOAA HTTPS into a tempfile (~20 MB).
     Local staging is ~2.8× faster than streaming `/vsicurl/` because
     NOAA's source TIFFs are one-row strips (uncompressed, blockysize=1)
     — pathological for HTTP range reads. The tempfile is deleted in a
     ``finally`` block so peak disk = workers × 20 MB ≈ 120 MB total.
  2. Classify at native 375 m → keep flood_fraction only.
  3. Harmonise to 1-arcmin global grid via Harmoniser.
  4. Scale float32 [0, 1] → uint8 [0, 100], NaN → 255.
  5. Write a true COG into an in-memory MemoryFile (output is ~2-3 KB).
  6. Upload bytes to s3://atlantis/{dest_key}.

The COG output stays in-memory because each result is tiny; only the
~20 MB input warrants local staging.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import numpy as np
import rasterio
import requests
from loguru import logger
from rasterio.io import MemoryFile

from atlantis.batch import TaskResult
from atlantis.config import HarmoniseConfig
from atlantis.fetchers.viirs.dataset import processed_tile_to_dataset
from atlantis.fetchers.viirs.processor import classify_viirs_pixels
from atlantis.harmoniser import HARMONISED_NODATA, Harmoniser

#: ECMWF object store endpoint.
_S3_ENDPOINT = "https://object-store.os-api.cci1.ecmwf.int"

#: Allow GDAL to use multiple internal threads for warping + COG encoding.
#: This is the C-level thread pool — independent of the Python GIL — so it
#: does not conflict with Dask's "1 Python thread per worker" model.
os.environ.setdefault("GDAL_NUM_THREADS", "2")

#: COG write profile.
_COG_PROFILE = {
    "driver": "COG",
    "dtype": "uint8",
    "nodata": HARMONISED_NODATA,
    "compress": "DEFLATE",
    "blocksize": 512,
    "predictor": 2,
    "overview_resampling": "average",
    "overviews": "AUTO",
}

#: HTTP chunk size for streaming downloads.
_DOWNLOAD_CHUNK_SIZE = 1 << 20  # 1 MiB


def _s3fs_filesystem():
    """Return an s3fs filesystem for the ECMWF object store.

    Instantiated inside the worker process (not at module level) so each
    Dask worker owns its own connection pool.
    """
    import s3fs

    return s3fs.S3FileSystem(
        endpoint_url=_S3_ENDPOINT,
        # Credentials come from ~/.aws/config 'default' profile written by `atlantis setup`.
    )


def _download_to_tempfile(url: str, suffix: str = ".tif") -> Path:
    """Stream *url* to a NamedTemporaryFile and return its path.

    The caller is responsible for deleting the file when done.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="viirs_src_")
    try:
        with os.fdopen(fd, "wb") as fp:
            with requests.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK_SIZE):
                    if chunk:
                        fp.write(chunk)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    return Path(tmp_path)


def process_granule(task: dict) -> TaskResult:
    """Process a single VIIRS granule end-to-end.

    Args:
        task: Dict with keys ``task_id``, ``source_uri``, ``dest_key``,
              ``date``, ``aoi_id`` (as produced by ``inventory.to_tasks()``).

    Returns:
        :class:`~atlantis.batch.TaskResult` on success.

    Raises:
        Exception: Any download / rasterio / harmoniser / s3fs error
            propagates to Dask so it counts against the retry budget.
    """
    task_id: str = task["task_id"]
    source_uri: str = task["source_uri"]
    dest_key: str = task["dest_key"]

    t0 = time.monotonic()
    src_path: Path | None = None
    try:
        # ── Step 1: download raw granule to a tempfile ────────────────────
        src_path = _download_to_tempfile(source_uri)

        # ── Step 2: read + classify ───────────────────────────────────────
        with rasterio.open(src_path) as src:
            data = src.read(1)
            transform = src.transform
            crs = src.crs.to_string() if src.crs else "EPSG:4326"

        processed = classify_viirs_pixels(data, transform, crs)

        # Build a minimal xarray Dataset with just flood_fraction.
        ds = processed_tile_to_dataset(
            processed,
            event_id=task.get("date", ""),
            source_id="viirs",
        )
        ds = ds[["flood_fraction"]]

        # ── Step 3: harmonise to 1 arcmin ────────────────────────────────
        harmoniser = Harmoniser(HarmoniseConfig())
        ds_harm = harmoniser.harmonise(ds, source_id="viirs", flood_variable="flood_fraction")

        # ── Step 4: scale float32 [0,1] → uint8 [0,100], NaN → 255 ──────
        arr = ds_harm["flood_fraction"].values
        scaled = np.where(
            np.isnan(arr),
            np.uint8(HARMONISED_NODATA),
            np.round(arr * 100).clip(0, 100).astype(np.uint8),
        )

        harm_da = ds_harm["flood_fraction"]
        harm_transform = harm_da.rio.transform()
        harm_crs = harm_da.rio.crs
        height, width = scaled.shape

        # ── Step 5: write COG into MemoryFile (output is ~2-3 KB) ───────
        profile = {
            **_COG_PROFILE,
            "count": 1,
            "height": height,
            "width": width,
            "crs": harm_crs,
            "transform": harm_transform,
        }
        with MemoryFile() as mem:
            with mem.open(**profile) as dst:
                dst.write(scaled, 1)
            cog_bytes = mem.read()

        # ── Step 6: upload to s3://atlantis/{dest_key} ────────────────────
        dest_uri = f"s3://atlantis/{dest_key}"
        fs = _s3fs_filesystem()
        with fs.open(dest_uri, "wb") as f:
            f.write(cog_bytes)

        elapsed = time.monotonic() - t0
        logger.debug("processed {} in {:.1f}s → {}", task_id, elapsed, dest_uri)

        return TaskResult(task_id=task_id, output_uri=dest_uri)
    finally:
        if src_path is not None:
            src_path.unlink(missing_ok=True)
