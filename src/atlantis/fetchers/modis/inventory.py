"""Inventory loader and task builder for MODIS MCDWD batch processing.

Reads the Parquet catalog (built by :mod:`atlantis.fetchers.modis.catalog`)
and converts it to task dicts consumed by the batch engine.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

DEFAULT_CATALOGUE_URI = "s3://atlantis/assets/modis/modis_archive_catalog.parquet"


def load_inventory(uri: str | Path) -> pd.DataFrame:
    """Load the MODIS catalog from a Parquet file.

    Args:
        uri: Path or S3 URI to the catalog Parquet file.

    Returns:
        DataFrame with columns: ``date``, ``h``, ``v``, ``task_id``, ``source_uri``.
    """
    uri_str = str(uri)
    return pd.read_parquet(uri_str, engine="pyarrow")


def slice_partition(df: pd.DataFrame, partition: str | None) -> pd.DataFrame:
    """Return a deterministic row slice of the catalog.

    Rows are sorted by ``(date, h, v)`` so contiguous date ranges are
    contiguous row ranges.

    Args:
        df: Full catalog DataFrame.
        partition: Slice string in ``"start:stop"`` form (e.g. ``"0:10000"``).
            ``None`` returns the full sorted frame.

    Returns:
        Sliced (and sorted) DataFrame.

    Raises:
        ValueError: If *partition* cannot be parsed or indices are out of range.
    """
    df = df.sort_values(["date", "h", "v"], ignore_index=True)
    if partition is None:
        return df
    try:
        start_str, stop_str = partition.split(":")
        start, stop = int(start_str), int(stop_str)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"partition must be 'start:stop' (e.g. '0:10000'), got: {partition!r}") from exc
    if start < 0 or stop > len(df) or start >= stop:
        raise ValueError(f"partition '{partition}' out of range for DataFrame of length {len(df)}")
    return df.iloc[start:stop].reset_index(drop=True)


def to_tasks(df: pd.DataFrame) -> list[dict]:
    """Convert a catalog DataFrame to a list of task dicts for the batch engine.

    Each task dict contains:
    - ``task_id``: unique ID e.g. ``modis-20200101-h08v05``
    - ``source_uri``: LAADS/LANCE download URL
    - ``date``: tile date string (``YYYY-MM-DD``)
    - ``h``: MODIS horizontal tile index
    - ``v``: MODIS vertical tile index

    Args:
        df: Catalog DataFrame (already sliced / filtered as needed).

    Returns:
        List of task dicts, one per row.
    """
    tasks = []
    for row in df.itertuples(index=False):
        tasks.append(
            {
                "task_id": row.task_id,
                "source_uri": row.source_uri,
                "date": row.date,
                "h": int(row.h),
                "v": int(row.v),
            }
        )
    return tasks
