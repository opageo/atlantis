"""Per-tile processing function for MODIS MCDWD cube batch runs.

``harmonise_modis_granule_payload`` is a top-level, picklable function —
no closures, no lambdas — so Dask can ship it to worker processes.

Pipeline per tile:
  1. Download the MODIS tile (HDF4 from LAADS or GeoTIFF from LANCE) → tempfile.
  2. Extract the F2 subdataset (from HDF4) or open the GeoTIFF → raw pixel codes.
  3. Derive ``water_fraction`` and ``recurring_flood`` via the MODIS registry.
  4. Build a georeferenced dataset (from ``tile_bounds_from_hv``).
  5. Harmonise to 1-arcmin global grid → reprojected layers + masks.
  6. Encode all 4 layers to uint8.
  7. Return the payload dict for the cube-batch coordinator.
"""

from __future__ import annotations

import gc
import os
import tempfile
from pathlib import Path

import numpy as np
import rasterio
import requests
from rasterio.transform import from_bounds

from atlantis.config import HarmoniseConfig
from atlantis.fetchers.modis.layers import SELECTED_COMPOSITE, registry
from atlantis.fetchers.modis.processor import (
    MODIS_TILE_PIXELS,
    find_hdf4_subdataset,
    tile_bounds_from_hv,
)
from atlantis.harmoniser import HARMONISED_NODATA, Harmoniser
from atlantis.layers import DerivationContext

os.environ.setdefault("GDAL_NUM_THREADS", "2")

_DOWNLOAD_CHUNK_SIZE = 1 << 20  # 1 MiB
_HDF4_SUFFIX = ".hdf"


def _download_to_tempfile(url: str, suffix: str = ".hdf") -> Path:
    """Stream *url* to a NamedTemporaryFile and return its path."""
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="modis_src_")
    try:
        with os.fdopen(fd, "wb") as fp:
            with requests.get(url, stream=True, timeout=120) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK_SIZE):
                    if chunk:
                        fp.write(chunk)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    return Path(tmp_path)


def _encode_uint8_values(values: np.ndarray) -> np.ndarray:
    """Encode a 2-D slice to uint8 storage.

    Float in [0, 1] → [0, 100] (percent), NaN → 255 nodata.
    Integer masks pass through.
    """
    if np.issubdtype(values.dtype, np.floating):
        return np.where(np.isnan(values), HARMONISED_NODATA, np.clip(np.round(values * 100), 0, 100)).astype("uint8")
    return values.astype("uint8")


def _water_fraction_dataarray(
    water_fraction: np.ndarray,
    transform: rasterio.Affine,
    crs: str,
):
    """Wrap a water_fraction array into a georeferenced rioxarray DataArray."""
    import rioxarray  # noqa: F401
    import xarray as xr

    height, width = water_fraction.shape
    x_coords = transform.c + (np.arange(width) + 0.5) * transform.a
    y_coords = transform.f + (np.arange(height) + 0.5) * transform.e
    da = xr.DataArray(
        water_fraction,
        dims=("y", "x"),
        coords={"y": y_coords, "x": x_coords},
        name="water_fraction",
    )
    da.rio.write_crs(crs, inplace=True)
    da.rio.write_transform(transform, inplace=True)
    return da


def _open_modis_tile(src_path: Path) -> tuple[np.ndarray, rasterio.Affine, str]:
    """Open a MODIS tile and return ``(raw_codes, transform, crs)``.

    Handles both HDF4 (LAADS) and GeoTIFF (LANCE) inputs.
    """
    if src_path.suffix.lower() == _HDF4_SUFFIX:
        subdataset_uri = find_hdf4_subdataset(src_path, "F2")
        with rasterio.open(subdataset_uri) as src:
            data = src.read(1).astype(np.uint8)
            transform = src.transform
            crs = src.crs.to_string() if src.crs else "EPSG:4326"
            if transform == rasterio.Affine.identity():
                hv = None
                try:
                    import re

                    match = re.search(r"\.h(\d{2})v(\d{2})\.", src_path.name)
                    if match:
                        hv = (int(match.group(1)), int(match.group(2)))
                except Exception:
                    pass
                if hv is None:
                    raise RuntimeError(f"Cannot determine georeferencing for HDF4 {src_path.name}")
                west, south, east, north = tile_bounds_from_hv(*hv)
                transform = from_bounds(west, south, east, north, MODIS_TILE_PIXELS, MODIS_TILE_PIXELS)
                crs = "EPSG:4326"
    else:
        with rasterio.open(src_path) as src:
            data = src.read(1).astype(np.uint8)
            transform = src.transform
            crs = src.crs.to_string() if src.crs else "EPSG:4326"
    return data, transform, crs


def harmonise_modis_granule_payload(task: dict) -> dict:
    """Download + derive + harmonise a MODIS tile, returning all cube layers in-memory.

    Pipeline: download → extract F2 subdataset → derive water_fraction + recurring_flood
    via MODIS registry → build georeferenced dataset (tile_bounds_from_hv) →
    harmonise (reproject + generate masks) → encode to uint8 → return payload.

    Args:
        task: Task dict with ``task_id``, ``source_uri``, ``date``, ``h``, ``v``.

    Returns:
        Dict with ``task_id``, ``date``, ``h``, ``v``, ``water_fraction``,
        ``exclusion_mask``, ``reference_water``, ``recurring_flood`` (all uint8),
        and ``y`` / ``x`` global-grid pixel centres.
    """
    import xarray as xr

    src_path: Path | None = None
    try:
        src_path = _download_to_tempfile(task["source_uri"])

        raw_codes, _, _ = _open_modis_tile(src_path)

        ctx = DerivationContext(arrays={SELECTED_COMPOSITE: raw_codes})
        water_fraction = registry.get_derived("water_fraction").derive(ctx)
        recurring_flood = registry.get_derived("recurring_flood").derive(ctx)
        del raw_codes

        h, v = task["h"], task["v"]
        west, south, east, north = tile_bounds_from_hv(h, v)
        transform = from_bounds(west, south, east, north, MODIS_TILE_PIXELS, MODIS_TILE_PIXELS)
        crs = "EPSG:4326"

        wf_da = _water_fraction_dataarray(water_fraction, transform, crs)
        rf_da = xr.DataArray(
            recurring_flood,
            dims=("y", "x"),
            coords={"y": wf_da["y"], "x": wf_da["x"]},
            name="recurring_flood",
        )
        rf_da.rio.write_crs(crs, inplace=True)
        rf_da.rio.write_transform(transform, inplace=True)

        ds = xr.Dataset({"water_fraction": wf_da, "recurring_flood": rf_da})
        del water_fraction, recurring_flood, wf_da, rf_da

        harmoniser = Harmoniser(HarmoniseConfig())
        ds_harm = harmoniser.harmonise(ds, source_id="modis", flood_variable="water_fraction")
        del ds

        wf = _encode_uint8_values(ds_harm["water_fraction"].values)
        em = _encode_uint8_values(ds_harm["exclusion_mask"].values)
        rw = _encode_uint8_values(ds_harm["reference_water"].values)
        rf = _encode_uint8_values(ds_harm["recurring_flood"].values)

        return {
            "task_id": task["task_id"],
            "date": task["date"],
            "h": h,
            "v": v,
            "water_fraction": wf,
            "exclusion_mask": em,
            "reference_water": rw,
            "recurring_flood": rf,
            "y": np.asarray(ds_harm["y"].values, dtype="float64"),
            "x": np.asarray(ds_harm["x"].values, dtype="float64"),
        }
    finally:
        if src_path is not None:
            src_path.unlink(missing_ok=True)
        gc.collect()
