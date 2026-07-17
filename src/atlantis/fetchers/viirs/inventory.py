"""Inventory loader and task builder for VIIRS JPSS batch processing.

Reads the Parquet catalogue from ``s3://atlantis/assets/viirs/jpss/2020/catalogue.parquet``
(or a local copy) and converts it to the task dicts consumed by the batch engine.
The load/slice mechanics are shared with every other source via
:mod:`atlantis.batch.catalog` — this module only supplies the VIIRS-specific
default URI, sort keys, and task schema.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from atlantis.batch.catalog import load_catalogue
from atlantis.batch.catalog import slice_partition as _slice_partition

#: Default S3 location of the JPSS 2020 catalogue.
DEFAULT_CATALOGUE_URI = "s3://atlantis/assets/viirs/jpss/2020/catalogue.parquet"

#: NOAA public S3 base URL.
NOAA_BASE_URL = "https://noaa-jpss.s3.amazonaws.com"

#: Sort keys that make row partitions contiguous for VIIRS's catalogue.
_SORT_KEYS = ("date", "aoi_id")


def load_inventory(uri: str | Path = DEFAULT_CATALOGUE_URI) -> pd.DataFrame:
    """Load the VIIRS JPSS catalogue from a Parquet file.

    Accepts both a local filesystem path and an ``s3://`` URI.  For S3 URIs
    the file is fetched through ``s3fs`` so the ECMWF custom endpoint
    (``https://object-store.os-api.cci1.ecmwf.int``) is used instead of
    pyarrow's built-in S3 filesystem, which does not know about it.

    Args:
        uri: Path or S3 URI to the catalogue Parquet file.

    Returns:
        DataFrame with columns: ``date``, ``aoi_id``, ``s3_key``, ``geometry``.
    """
    return load_catalogue(uri)


def slice_partition(df: pd.DataFrame, partition: str | None) -> pd.DataFrame:
    """Return a deterministic row slice of the catalogue.

    Rows are first sorted by ``(date, aoi_id)`` so the slice is reproducible
    across machines and re-runs.

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


def to_tasks(df: pd.DataFrame, output_prefix: str = "viirs/jpss/2020") -> list[dict]:
    """Convert a catalogue DataFrame to a list of task dicts for the batch engine.

    Each task dict contains:
    - ``task_id``: filename stem of the NOAA granule (unique per row).
    - ``source_uri``: full NOAA HTTPS URL for ``/vsicurl/`` streaming.
    - ``dest_key``: S3 key under ``s3://atlantis/`` for the output COG.
    - ``date``: granule date string (``YYYY-MM-DD``).
    - ``aoi_id``: integer AOI tile identifier.

    Args:
        df: Catalogue DataFrame (already sliced / filtered as needed).
        output_prefix: S3 key prefix for outputs (without leading ``s3://atlantis/``).

    Returns:
        List of task dicts, one per row.
    """
    tasks = []
    prefix = output_prefix.rstrip("/")
    for row in df.itertuples(index=False):
        task_id = Path(row.s3_key).stem
        source_uri = f"{NOAA_BASE_URL}/{row.s3_key}"
        dest_key = f"{prefix}/{row.date}/GLB{int(row.aoi_id):03d}.tif"
        tasks.append(
            {
                "task_id": task_id,
                "source_uri": source_uri,
                "dest_key": dest_key,
                "date": row.date,
                "aoi_id": int(row.aoi_id),
            }
        )
    return tasks
