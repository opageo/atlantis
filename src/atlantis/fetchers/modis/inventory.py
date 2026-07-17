"""Inventory loader and task builder for MODIS MCDWD batch processing.

Reads the Parquet catalogue produced by
:func:`atlantis.fetchers.modis.catalog.build_catalog` (a local file or an
``s3://`` URI) and converts it to the task dicts consumed by the cube batch
engine.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

#: Default S3 location of the MODIS MCDWD tile catalog.
DEFAULT_CATALOGUE_URI = "s3://atlantis/assets/modis/modis_archive_catalog.parquet"

#: ECMWF object store endpoint (used for reading catalogue Parquet from S3).
_S3_ENDPOINT = "https://object-store.os-api.cci1.ecmwf.int"


def load_inventory(uri: str | Path = DEFAULT_CATALOGUE_URI) -> pd.DataFrame:
    """Load the MODIS MCDWD catalogue from a Parquet file.

    Accepts both a local filesystem path and an ``s3://`` URI. For S3 URIs the
    file is fetched through ``s3fs`` so the ECMWF custom endpoint
    (``https://object-store.os-api.cci1.ecmwf.int``) is used instead of
    pyarrow's built-in S3 filesystem, which does not know about it.

    Args:
        uri: Path or S3 URI to the catalogue Parquet file.

    Returns:
        DataFrame with columns: ``date``, ``h``, ``v``, ``task_id``, ``source_uri``.
    """
    uri_str = str(uri)
    if uri_str.startswith("s3://"):
        import s3fs

        fs = s3fs.S3FileSystem(endpoint_url=_S3_ENDPOINT)
        with fs.open(uri_str, "rb") as f:
            return pd.read_parquet(f, engine="pyarrow")
    return pd.read_parquet(uri_str, engine="pyarrow")


def slice_partition(df: pd.DataFrame, partition: str | None) -> pd.DataFrame:
    """Return a deterministic row slice of the catalogue.

    Rows are first sorted by ``(date, h, v)`` so the slice is reproducible
    across machines and re-runs and so contiguous date ranges are contiguous
    row ranges — important for partitioning a multi-year ingestion across
    machines.

    Args:
        df: Full catalogue DataFrame.
        partition: Slice string in ``"start:stop"`` form (e.g. ``"0:24464"``).
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
        raise ValueError(f"partition must be 'start:stop' (e.g. '0:24464'), got: {partition!r}") from exc
    if start < 0 or stop > len(df) or start >= stop:
        raise ValueError(f"partition '{partition}' out of range for DataFrame of length {len(df)}")
    return df.iloc[start:stop].reset_index(drop=True)


def to_tasks(df: pd.DataFrame) -> list[dict]:
    """Convert a catalogue DataFrame to a list of task dicts for the batch engine.

    Each task dict contains:

    - ``task_id``: unique per row, e.g. ``modis-20200101-h08v05``.
    - ``source_uri``: full NASA LAADS download URL for the tile.
    - ``date``: granule date string (``YYYY-MM-DD``).
    - ``h``, ``v``: MODIS tile coordinates (0–35 / 0–17).

    Args:
        df: Catalogue DataFrame (already sliced / filtered as needed).

    Returns:
        List of task dicts, one per row.
    """
    tasks: list[dict] = []
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
