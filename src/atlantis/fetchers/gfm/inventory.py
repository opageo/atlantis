"""Inventory loader and task builder for GFM batch processing.

Reads the Parquet catalogue produced by
:func:`atlantis.fetchers.gfm.catalog.build_catalog` (one row per STAC item)
and converts it to the task dicts consumed by the cube batch engine. The
load/slice mechanics are shared with every other source via
:mod:`atlantis.batch.catalog` — this module only supplies the GFM-specific
default URI, sort keys, and task schema.

Unlike VIIRS/MODIS, more than one GFM STAC item can share the same
``(date, equi7_tile)`` cell (e.g. ascending + descending Sentinel-1 passes on
the same day). :func:`to_tasks` groups those rows into a single accumulating
task so the batch worker merges them via the existing multi-item
``GfmRasterProcessor.process_items`` accumulation, instead of one task
silently overwriting another's region-write in the Zarr cube.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from atlantis.batch.catalog import load_catalogue
from atlantis.batch.catalog import slice_partition as _slice_partition

#: Default S3 location of the GFM STAC-item catalog.
DEFAULT_CATALOGUE_URI = "s3://atlantis/assets/gfm/gfm_archive_catalog.parquet"

#: Sort keys that make row partitions contiguous for GFM's catalogue.
_SORT_KEYS = ("date", "equi7_tile")


def load_inventory(uri: str | Path = DEFAULT_CATALOGUE_URI) -> pd.DataFrame:
    """Load the GFM STAC-item catalogue from a Parquet file.

    Accepts both a local filesystem path and an ``s3://`` URI. For S3 URIs the
    file is fetched through ``s3fs`` so the ECMWF custom endpoint
    (``https://object-store.os-api.cci1.ecmwf.int``) is used instead of
    pyarrow's built-in S3 filesystem, which does not know about it.

    Args:
        uri: Path or S3 URI to the catalogue Parquet file.

    Returns:
        DataFrame with columns: ``date``, ``equi7_tile``, ``item_id``,
        ``item_href``, ``west``, ``south``, ``east``, ``north`` — one row per
        STAC item.
    """
    return load_catalogue(uri)


def slice_partition(df: pd.DataFrame, partition: str | None) -> pd.DataFrame:
    """Return a deterministic row slice of the catalogue.

    Rows are sliced at individual STAC-item granularity — sorted by
    ``(date, equi7_tile)`` first so the slice is reproducible — **before**
    the ``(date, equi7_tile)`` grouping in :func:`to_tasks`. A cell whose
    items straddle a partition boundary will only partially accumulate on
    each side; this is a known, low-impact limitation (see cube-build docs).

    Args:
        df: Full catalogue DataFrame.
        partition: Slice string in ``"start:stop"`` form (e.g. ``"0:24464"``).
            ``None`` returns the full sorted frame.

    Returns:
        Sliced (and sorted) DataFrame.

    Raises:
        ValueError: If *partition* cannot be parsed or indices are out of range.
    """
    return _slice_partition(df, partition, _SORT_KEYS)


def to_tasks(df: pd.DataFrame) -> list[dict]:
    """Convert a catalogue DataFrame to a list of task dicts for the batch engine.

    Groups rows by ``(date, equi7_tile)`` since a cell can have more than one
    GFM STAC item (e.g. ascending + descending Sentinel-1 passes) — each task
    carries every item href for that cell so the batch worker processes them
    as one accumulating unit via ``GfmRasterProcessor.process_items``, the
    same multi-item merge the interactive fetcher already relies on.

    Each task dict contains:

    - ``task_id``: unique per ``(date, equi7_tile)``, e.g.
      ``gfm-20241101-EU020M_E036N009T3``.
    - ``date``: acquisition date string (``YYYY-MM-DD``).
    - ``equi7_tile``: EQUI7 tile id (the GFM analog of MODIS's ``h``/``v``).
    - ``item_hrefs``: list of STAC item self-hrefs sharing this cell.
    - ``bbox``: ``(west, south, east, north)`` — union of the group's item bboxes.

    Args:
        df: Catalogue DataFrame (already sliced / filtered as needed).

    Returns:
        List of task dicts, one per ``(date, equi7_tile)`` group.
    """
    tasks: list[dict] = []
    for (day, tile), group in df.groupby(["date", "equi7_tile"], sort=False):
        tasks.append(
            {
                "task_id": f"gfm-{str(day).replace('-', '')}-{tile}",
                "date": day,
                "equi7_tile": tile,
                "item_hrefs": list(group["item_href"]),
                "bbox": (
                    float(group["west"].min()),
                    float(group["south"].min()),
                    float(group["east"].max()),
                    float(group["north"].max()),
                ),
            }
        )
    return tasks
