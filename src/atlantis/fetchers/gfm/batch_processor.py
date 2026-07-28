"""Per-cell processing function for GFM cube batch runs.

``harmonise_gfm_payload`` is a top-level, picklable function — no closures, no
lambdas — so Dask can ship it to worker processes without issue.

Unlike VIIRS/MODIS, GFM has no discrete "download the raw file" step: pixel
data is always streamed live from EODC's Cloud-Optimised GeoTIFFs via
``odc.stac`` inside :class:`~atlantis.fetchers.gfm.processor.GfmRasterProcessor`.
This function only needs to re-fetch the (small) STAC item JSON for each href
recorded on the task and hand the resulting :class:`pystac.Item` objects to
the existing single-event processing pipeline, unchanged.

Pipeline per ``(date, equi7_tile)`` task:

  1. Re-fetch each STAC item referenced by the task's ``item_hrefs``.
  2. Run them through :meth:`GfmRasterProcessor.process_items` — the same
     multi-item accumulation the interactive fetcher already uses, so items
     sharing this cell (e.g. ascending + descending Sentinel-1 passes) are
     merged rather than overwriting each other.
  3. Convert the result to a dataset via the existing
     :func:`~atlantis.fetchers.gfm.dataset.processed_tile_to_dataset` helper.
  4. Keep only the layers persisted in the shared cube (``water_fraction``,
     ``exclusion_mask``, ``reference_water``) and harmonise via
     :class:`~atlantis.harmoniser.Harmoniser` — the same call already proven
     by the interactive ``--harmonise`` path.
  5. Encode each layer to uint8 and return them + global-grid coordinates so
     a coordinator can region-write them into the consolidated Zarr datacube.
"""

from __future__ import annotations

import gc
import os
import sys

import numpy as np
from loguru import logger
from rasterio.enums import Resampling

from atlantis.archive.writer import _encode_uint8
from atlantis.config import HarmoniseConfig, get_config
from atlantis.fetchers.gfm.dataset import processed_tile_to_dataset
from atlantis.fetchers.gfm.processor import GfmRasterProcessor
from atlantis.harmoniser import Harmoniser

#: Bound GDAL's per-process raster block cache (MB). Unlike MODIS/VIIRS, GFM
#: streams pixel data live from many distinct COGs per worker lifetime (no
#: discrete "download" step), so an unbounded cache grows without limit and
#: triggers distributed.worker.memory "Unmanaged memory... high" / "Pausing
#: worker" warnings. Mirrors the ``GDAL_NUM_THREADS`` module-level pattern in
#: atlantis.fetchers.modis.batch_processor / atlantis.fetchers.viirs.batch_processor.
os.environ.setdefault("GDAL_CACHEMAX", "256")

#: Cube-persisted GFM layers — matches the shared ArchiveWriter schema.
#: ``ensemble_likelihood`` / ``advisory_flags`` are native-only companions and
#: are not stored in the cube (see docs/layers.md).
_CUBE_LAYERS = ("water_fraction", "exclusion_mask", "reference_water")


def _trim_malloc() -> None:
    """Release freed native heap memory back to the OS on glibc Linux.

    ``gc.collect()`` only reclaims Python-tracked objects; native allocations
    made by GDAL/numpy (malloc'd C buffers) are freed by libc but often kept
    in the process heap rather than returned to the OS, which is what the
    distributed.worker.memory "memory not released back to the OS" guidance
    (see distributed.dask.org .../worker-memory.html) recommends working
    around with ``malloc_trim``. Best-effort only: silently a no-op on
    non-Linux platforms or if libc doesn't expose ``malloc_trim``.

    Note: in an isolated synthetic benchmark (large numpy buffers, no GDAL/
    network I/O) this showed no measurable effect beyond ``gc.collect()``
    alone — glibc routes large allocations through ``mmap``, which is already
    released on ``free()``. Kept as cheap, harmless defense-in-depth for the
    smaller ``brk``-heap allocations it *does* help with; the real fix for
    GFM's inter-cell RSS growth is ``gdal.VSICurlClearCache()`` in the caller
    (confirmed via a real multi-cell A/B — see
    :func:`harmonise_gfm_payload`'s cleanup block).
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass


def harmonise_gfm_payload(task: dict) -> dict:
    """Load + accumulate + harmonise one ``(date, equi7_tile)`` GFM cell.

    The picklable produce function for the GFM cube batch. Mirrors
    :func:`atlantis.fetchers.viirs.batch_processor.harmonise_granule_payload`
    and
    :func:`atlantis.fetchers.modis.batch_processor.harmonise_modis_granule_payload`:
    Dask workers run this in parallel, the coordinator streams each payload
    into the consolidated Zarr datacube.

    Args:
        task: Task dict (``task_id``, ``date``, ``equi7_tile``, ``item_hrefs``,
            ``bbox``) as produced by
            :func:`atlantis.fetchers.gfm.inventory.to_tasks`.

    Returns:
        Dict with ``task_id``, ``date``, ``equi7_tile``, the harmonised
        ``water_fraction`` (uint8 ``[0, 100]`` / 255 nodata), ``exclusion_mask``
        (uint8, GFM's native exclusion codes — not synthesized), and
        ``reference_water`` (uint8, GFM's native 3-class codes) on the
        canonical 1-arcmin grid, plus ``y`` / ``x`` global pixel centres for
        the harmonised cell window.

    Raises:
        RuntimeError: If no STAC item in the task yielded valid data.
        Exception: Any STAC / rasterio / GDAL / harmoniser error propagates
            to Dask so it counts against the retry budget.
    """
    import pystac

    task_id: str = task["task_id"]
    date_str: str = str(task["date"])
    equi7_tile: str = task["equi7_tile"]
    bbox: tuple[float, float, float, float] = tuple(task["bbox"])

    cfg = get_config().fetcher
    coarsen_factor = cfg.gfm_coarsen_factor
    resampling = Resampling[cfg.gfm_resampling]
    window_size = cfg.gfm_window_size

    items = [pystac.Item.from_file(href) for href in task["item_hrefs"]]

    processor = GfmRasterProcessor(
        bbox=bbox,
        coarsen_factor=coarsen_factor,
        resampling=resampling,
        classify=True,
        max_retries=cfg.max_retries,
        window_size=window_size,
    )
    result = processor.process_items(
        items,
        event_id="",
        date_token=date_str.replace("-", ""),
        output_dir=None,
        write_outputs=False,
    )
    if result is None:
        raise RuntimeError(f"No valid GFM data for task {task_id} ({len(items)} item(s))")

    ds = processed_tile_to_dataset(result.processed, event_id="", source_id="gfm")
    ds = ds[[name for name in _CUBE_LAYERS if name in ds.data_vars]]
    del result

    harmoniser = Harmoniser(HarmoniseConfig())
    ds_harm = harmoniser.harmonise(ds, source_id="gfm", flood_variable="water_fraction")
    del ds

    water_u8 = _encode_uint8(ds_harm["water_fraction"].values)
    exclusion_u8 = ds_harm["exclusion_mask"].values.astype(np.uint8)
    reference_u8 = ds_harm["reference_water"].values.astype(np.uint8)
    y = np.asarray(ds_harm["y"].values, dtype="float64")
    x = np.asarray(ds_harm["x"].values, dtype="float64")
    del ds_harm
    gc.collect()
    _trim_malloc()
    # Force GDAL to release accumulated raster block caches and COG file
    # handles between cells — without this, the per-worker RSS climbs across
    # successive cells even though each individual cell's processing delta is
    # only ~2 GiB. Cumulative GDAL cache accumulation across ~20+ COG reads
    # per cell × N cells can push the absolute worker RSS from ~2.5 GiB to
    # ~6 GiB, triggering Dask's 80% pause/resume threshold.
    #
    # gdal.dump_open_datasets() does NOT exist in GDAL >= 3.10 (it's a GDAL 2.x
    # API removed from the Python bindings) — the old call here was a silent
    # no-op (its AttributeError was swallowed). Replaced with the real GDAL 3.x
    # equivalent:
    #   SetCacheMax(0)  — flush + zero the raster block cache
    #   SetCacheMax(N)  — restore the module-level cap (GDAL_CACHEMAX MB → bytes)
    #   VSICurlClearCache() — clear HTTP range-request / COG header caches for
    #                         the many distinct remote COG URLs GFM streams per
    #                         cell (unlike VIIRS/MODIS, which download once)
    #
    # This block's own effect is CONFIRMED real at single-process scale against
    # real EODC data, not just plausible: a 6-cell real-item A/B
    # (scripts/profile_gfm_batch_rss.py, cleanup on vs. monkeypatched no-op,
    # isolated subprocesses) showed live VmRSS bounded at ~330-380 MiB across
    # all 6 EU cells with this block, vs. climbing to ~1.5 GiB (~4x higher)
    # without it. VSICurlClearCache() is the component doing the real work — a
    # synthetic local-GeoTIFF test (no HTTP layer) showed SetCacheMax/
    # malloc_trim alone have no measurable effect once a dataset is closed.
    #
    # This cleanup complements, but does not replace, the nesting fix in
    # GfmRasterProcessor._load_item: odc.stac's inner Dask graph must execute
    # synchronously in the owning outer worker. With both protections in place,
    # the real default three-worker/8GB batch completed all 48 cells in the
    # Africa-heavy 2025 catalogue validation (DONE=48, FAILED=0; issue #96).
    try:
        from osgeo import gdal

        gdal.SetCacheMax(0)
        gdal.SetCacheMax(256 * 1024 * 1024)
        gdal.VSICurlClearCache()
    except Exception:
        pass

    logger.debug("gfm cell {} {} → shape {}", task_id, equi7_tile, water_u8.shape)
    return {
        "task_id": task_id,
        "date": date_str,
        "equi7_tile": equi7_tile,
        "water_fraction": water_u8,
        "exclusion_mask": exclusion_u8,
        "reference_water": reference_u8,
        "y": y,
        "x": x,
    }
