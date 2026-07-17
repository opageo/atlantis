"""Generic Parquet catalogue I/O and partitioning shared by every batch source.

Every source (VIIRS, MODIS, ...) builds its own catalogue of remote granules
or tiles and its own task dicts, but the underlying catalogue mechanics —
load a Parquet file (local or ``s3://``), deterministically slice it into a
row partition, write a freshly-built catalogue back out, walk an inclusive
date range, and retry a flaky HTTP listing call — are identical across
sources. This module holds that shared, dataset-agnostic core so each
``atlantis.fetchers.<source>.inventory`` / ``catalog`` module stays a thin,
source-specific wrapper (URL scheme, task schema, sort keys) around it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any, TypeVar

import pandas as pd
from loguru import logger

#: ECMWF object store endpoint (used for reading catalogue Parquet from S3).
DEFAULT_S3_ENDPOINT = "https://object-store.os-api.cci1.ecmwf.int"

T = TypeVar("T")


def load_catalogue(uri: str | Path, *, s3_endpoint: str = DEFAULT_S3_ENDPOINT) -> pd.DataFrame:
    """Load a catalogue Parquet file from a local path or an ``s3://`` URI.

    For S3 URIs the file is fetched through ``s3fs`` so *s3_endpoint* (the
    ECMWF custom object store, by default) is used instead of pyarrow's
    built-in S3 filesystem, which does not know about it.

    Args:
        uri: Path or S3 URI to the catalogue Parquet file.
        s3_endpoint: fsspec ``endpoint_url`` used for ``s3://`` reads.

    Returns:
        The catalogue as an unsorted DataFrame — callers sort/slice via
        :func:`slice_partition`.
    """
    uri_str = str(uri)
    if uri_str.startswith("s3://"):
        import s3fs

        fs = s3fs.S3FileSystem(endpoint_url=s3_endpoint)
        with fs.open(uri_str, "rb") as f:
            return pd.read_parquet(f, engine="pyarrow")
    return pd.read_parquet(uri_str, engine="pyarrow")


def slice_partition(df: pd.DataFrame, partition: str | None, sort_keys: tuple[str, ...]) -> pd.DataFrame:
    """Return a deterministic row slice of a catalogue.

    Rows are first sorted by *sort_keys* so the slice is reproducible across
    machines and re-runs, and so contiguous date ranges land in contiguous
    row ranges — important for splitting a multi-year ingestion across
    machines.

    Args:
        df: Full catalogue DataFrame.
        partition: Slice string in ``"start:stop"`` form (e.g. ``"0:24464"``).
            ``None`` returns the full sorted frame.
        sort_keys: Column names to sort by, in order (e.g.
            ``("date", "aoi_id")`` for VIIRS or ``("date", "h", "v")`` for
            MODIS).

    Returns:
        Sliced (and sorted) DataFrame.

    Raises:
        ValueError: If *partition* cannot be parsed or indices are out of range.
    """
    df = df.sort_values(list(sort_keys), ignore_index=True)
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


def write_catalogue(
    df: pd.DataFrame,
    output: str | Path,
    *,
    storage_options: dict[str, Any] | None = None,
) -> Path | None:
    """Write a freshly-built catalogue DataFrame to *output*.

    Args:
        df: Catalogue DataFrame to write.
        output: Output destination — a local path or an ``s3://`` URI.
        storage_options: fsspec options for S3 writes (e.g. ``endpoint_url``).

    Returns:
        The local :class:`~pathlib.Path` written, or ``None`` for an
        ``s3://`` destination.
    """
    output_str = str(output)
    if output_str.startswith("s3://"):
        import s3fs

        fs = s3fs.S3FileSystem(**(storage_options or {}))
        with fs.open(output_str, "wb") as f:
            df.to_parquet(f, engine="pyarrow", index=False)
        logger.info("Wrote catalogue → {}", output_str)
        return None
    local_path = Path(output_str)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(local_path, engine="pyarrow", index=False)
    logger.info("Wrote catalogue → {}", output_str)
    return local_path


def iter_dates(start: str, end: str) -> Iterator[date]:
    """Yield every calendar date from *start* to *end*, inclusive.

    Args:
        start: Start date ``YYYY-MM-DD``.
        end: End date ``YYYY-MM-DD`` (inclusive).

    Yields:
        Each :class:`datetime.date` in the range, in order.
    """
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    days = (end_date - start_date).days + 1
    for i in range(days):
        yield start_date + timedelta(days=i)


def log_progress(
    index: int,
    total: int,
    *,
    every: int = 30,
    label: str = "Progress",
    on_progress: Callable[[str], None] | None = None,
) -> None:
    """Report progress every *every* iterations, and on the last.

    Catalog builders walk hundreds to thousands of dates with no other
    per-item output (per-date detail is ``DEBUG``-only), so without this the
    process looks silent for hours. Call from inside an ``enumerate()`` loop.

    By default this emits via ``loguru`` (``logger.info``) — fine for library
    or test use, but the Atlantis CLI disables ``loguru`` output entirely
    unless ``--verbose`` is passed (see ``atlantis.cli._main``), which would
    silently swallow this. Pass *on_progress* (e.g. ``atlantis.utils.ui.info``)
    to route through a sink that's always visible instead.

    Args:
        index: Zero-based position of the current item.
        total: Total number of items in the loop.
        every: Emit a line every this many completed items.
        label: Prefix for the line (e.g. ``"MODIS catalog"``).
        on_progress: Optional sink to call with the formatted message instead
            of ``logger.info``. Keeps this module decoupled from any specific
            presentation layer (CLI, notebook, etc.).
    """
    completed = index + 1
    if completed % every == 0 or completed == total:
        message = f"{label}: {completed}/{total} ({100 * completed / total:.1f}%)"
        if on_progress is not None:
            on_progress(message)
        else:
            logger.info(message)


def retry_request(
    fn: Callable[[], T],
    *,
    max_retries: int = 5,
    backoff_base: float = 1.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    label: str = "",
) -> T:
    """Call *fn* with exponential-backoff retry on failure.

    Args:
        fn: Zero-argument callable to invoke (wrap the real call in a
            closure/lambda so this stays transport-agnostic).
        max_retries: Maximum number of attempts before the last exception
            propagates.
        backoff_base: Base delay in seconds; wait time is
            ``backoff_base * 2**attempt``.
        exceptions: Exception type(s) that trigger a retry. Anything else
            propagates immediately.
        label: Human-readable label used in the warning log line.

    Returns:
        Whatever *fn* returns.

    Raises:
        Exception: Whatever *fn* raises, on the final attempt (or
            immediately, if it is not one of *exceptions*).
    """
    for attempt in range(max_retries):
        try:
            return fn()
        except exceptions as exc:
            if attempt == max_retries - 1:
                raise
            wait = backoff_base * (2**attempt)
            logger.warning(
                "{} failed (attempt {}/{}): {}. Retrying in {:.0f}s …",
                label or "request",
                attempt + 1,
                max_retries,
                exc,
                wait,
            )
            time.sleep(wait)
    raise RuntimeError("unreachable")  # pragma: no cover
