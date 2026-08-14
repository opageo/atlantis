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

import os

import numpy as np
from loguru import logger
from rasterio.enums import Resampling

from atlantis.archive.writer import _encode_uint8
from atlantis.config import HarmoniseConfig, get_config
from atlantis.fetchers.gfm.dataset import processed_tile_to_dataset
from atlantis.fetchers.gfm.processor import GfmRasterProcessor, release_gdal_memory
from atlantis.harmoniser import Harmoniser

#: Bound GDAL's per-process raster block cache (MB). Unlike MODIS/VIIRS, GFM
#: streams pixel data live from many distinct COGs per worker lifetime (no
#: discrete "download" step), so the cache needs an explicit cap. The block
#: cache itself is LRU-bounded at this value; the multi-GB worker RSS growth
#: that motivated the cap comes from the glibc heap and retained dataset
#: handles, released per item by :func:`release_gdal_memory` (see
#: docs/gfm/memory-root-cause.md). Mirrors the ``GDAL_NUM_THREADS`` module-level
#: pattern in atlantis.fetchers.modis.batch_processor /
#: atlantis.fetchers.viirs.batch_processor.
os.environ.setdefault("GDAL_CACHEMAX", "256")

#: Cube-persisted GFM layers — matches the shared ArchiveWriter schema.
#: ``ensemble_likelihood`` / ``advisory_flags`` are native-only companions and
#: are not stored in the cube (see docs/layers.md).
_CUBE_LAYERS = ("water_fraction", "exclusion_mask", "reference_water")


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
    # Task-end release valve — belt-and-braces. The per-item cadence runs
    # inside GfmRasterProcessor._process_items_* via release_gdal_memory();
    # this final flush makes sure the worker hands a clean heap back to Dask
    # before the payload is serialised to the coordinator.
    #
    # Root cause summary (see docs/gfm/memory-root-cause.md): GDAL's raster
    # block cache (GDAL_CACHEMAX) and the /vsicurl/ HTTP range-request region
    # cache (CPL_VSIL_CURL_CACHE_SIZE, default 16 MB) are both LRU-BOUNDED —
    # they cannot grow into the multi-GB "unmanaged" RSS that killed 4 GB
    # workers. The real accumulator is glibc heap fragmentation plus
    # per-dataset native state retained while dataset handles stay open,
    # combined with a cleanup cadence that ran only once per task (fine for
    # 1-3-item cells, not for 23-item tasks). VSICurlClearCache() still helps:
    # a 6-cell real-item A/B (scripts/profile_gfm_batch_rss.py) bounded VmRSS
    # at ~330-380 MiB with the block vs ~1.5 GiB climbing without it — but the
    # durable fix is the per-item release + heap trimming now in the processor.
    #
    # Complements, but does not replace, the nesting fix in
    # GfmRasterProcessor._load_item: odc.stac's inner Dask graph must execute
    # synchronously in the owning outer worker.
    release_gdal_memory()

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
