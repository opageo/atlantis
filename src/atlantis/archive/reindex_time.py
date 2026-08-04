"""One-off offline reindex of a source group's time axis (append-only repair).

The update worker refuses to append a date older than the current axis tail
(policy 2). When a hole must be filled — or the axis was built out of order by
the completion-order batch engine — run this migration: it rewrites the group's
time-major arrays into strictly ascending order, inserting empty NODATA slots
for any expected dates missing from the axis, then swaps the group into place.

The rewrite goes through a temp group (``_<source>_sorted``) inside the same
store and swaps it with the original; on a remote store the swap copies the
group's materialised data once, so this is a deliberate one-off, not part of
the weekly path.
"""

from __future__ import annotations

import shutil
import time
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import s3fs
import zarr
from loguru import logger

from atlantis.archive import datacube

#: Fallback epoch if the group lacks CF ``units`` metadata.
_DEFAULT_EPOCH = "2020-01-01"

_TEMP_SUFFIX = "_sorted"


def read_group_epoch(group: zarr.Group) -> str:
    """Return the CF epoch encoded in the group's ``time`` units."""
    units = group["time"].attrs.get("units", f"days since {_DEFAULT_EPOCH}")
    return str(units).rsplit("since ", 1)[-1].strip()


def reindex_group_time(
    store: Any,
    source_id: str,
    var_names: list[str],
    *,
    expected_dates: list[date] | None = None,
    epoch: str | None = None,
    storage_options: dict[str, Any] | None = None,
) -> np.ndarray:
    """Rewrite *source_id*'s time axis strictly ascending, in place.

    Args:
        store: Datacube store — a local path or a Zarr store (see
            :func:`atlantis.archive._store.store_for`).
        source_id: Source group name (e.g. ``"modis"``).
        var_names: Time-major data arrays to reorder.
        expected_dates: Calendar dates that must exist on the axis; missing
            ones are inserted as empty NODATA slots. ``None`` sorts only.
        epoch: CF epoch for the integer time axis (read from the group when
            ``None``).
        storage_options: fsspec options for the remote-store swap (the
            store's own filesystem is async-mode, so the group swap uses a
            fresh synchronous filesystem).

    Returns:
        The new sorted time values (int days since epoch).

    Raises:
        ValueError: If the group does not exist in the store.
    """
    root = zarr.open_group(store, mode="a", use_consolidated=False)
    if source_id not in root:
        raise ValueError(f"no group {source_id!r} in store")
    group = root[source_id]
    epoch = epoch or read_group_epoch(group)
    times = np.asarray(group["time"][:], dtype="int64")

    if expected_dates:
        expected = np.asarray([datacube.date_to_int(d, epoch) for d in expected_dates], dtype="int64")
        target = np.sort(np.unique(np.concatenate([times, expected])))
    else:
        target = np.sort(times)
    if len(target) == len(times) and np.array_equal(target, times):
        return times

    tmp_name = f"_{source_id}{_TEMP_SUFFIX}"
    if tmp_name in root and _temp_is_valid(root[tmp_name], group, var_names, target):
        logger.info("Reusing existing temp group %s — swap only", tmp_name)
    else:
        # Old time index i lands at new position perm[i].
        perm = np.searchsorted(target, times)
        tmp = root.create_group(tmp_name, overwrite=True)

        for name in ("y", "x", "crs"):
            src = group[name]
            dst = tmp.create_array(name, shape=src.shape, chunks=src.chunks, dtype=src.dtype)
            dst[...] = src[...]
            dst.attrs.update(dict(src.attrs))

        for name in var_names:
            src = group[name]
            dst = tmp.create_array(
                name,
                shape=(len(target),) + tuple(src.shape[1:]),
                chunks=src.chunks,
                dtype=src.dtype,
                fill_value=src.fill_value,
            )
            dst.attrs.update(dict(src.attrs))
            for old_i in range(len(times)):
                dst[perm[old_i]] = np.asarray(src[old_i])

        t = tmp.create_array("time", shape=(len(target),), chunks=(512,), dtype="int64")
        t[:] = target
        t.attrs.update(dict(group["time"].attrs))
        tmp.attrs.update(dict(group.attrs))

        if not np.array_equal(np.asarray(t[:]), target):
            raise RuntimeError("reindex validation failed: temp group time axis is not sorted")

    _swap_group(store, source_id, tmp_name, storage_options)
    _consolidate_verified(store, source_id, len(target))
    return target


def _temp_is_valid(tmp: zarr.Group, group: zarr.Group, var_names: list[str], target: np.ndarray) -> bool:
    """True when a leftover temp group is complete and matches *target*.

    The temp group is only complete once its ``time`` array has been written
    (data arrays are filled first), so a matching time axis implies every
    plane was copied.
    """
    try:
        if "time" not in tmp:
            return False
        if not np.array_equal(np.asarray(tmp["time"][:], dtype="int64"), target):
            return False
        for name in var_names:
            if name not in tmp or tuple(tmp[name].shape) != (len(target),) + tuple(group[name].shape[1:]):
                return False
        return True
    except Exception:  # noqa: BLE001 - a broken temp group is simply re-copied
        return False


def _swap_group(store: Any, old: str, new: str, storage_options: dict[str, Any] | None = None) -> None:
    """Replace group *old* with group *new* (which is renamed to *old*).

    On a remote store the Zarr store's filesystem is async-mode (sync calls
    raise outside a running event loop), so a fresh synchronous filesystem is
    used for the swap. The promoted group is **copied onto** *old* (idempotent
    PUTs — *old* is never absent, so a reader never sees a missing group),
    then the file count is verified against the source with
    listing-consistency retries, and only then is the temp group removed.
    A silently-empty source listing (this store's listings lag) is treated as
    a failure and retried, never as "nothing to copy".
    """
    if isinstance(store, Path):
        old_dir, new_dir = store / old, store / new
        shutil.rmtree(old_dir)
        new_dir.rename(old_dir)
        return
    fs = s3fs.S3FileSystem(**(storage_options or {}))
    base = store.path
    src, dst = f"{base}/{new}", f"{base}/{old}"
    n = _count_files(fs, src)
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            fs.copy(src, dst, recursive=True, on_error="raise")
            # s3fs nests the source under an existing destination (copies
            # INTO the dir); the count check alone cannot tell — verify the
            # promoted group is not a nested copy before trusting it.
            if _wait_for_file_count(fs, dst, n) and not fs.exists(f"{dst}/{new}"):
                fs.rm(src, recursive=True)
                return
            nested = fs.exists(f"{dst}/{new}")
            last_error = RuntimeError(f"promote {new} -> {old}: {len(fs.find(dst))}/{n} files, nested={nested}")
        except Exception as exc:  # noqa: BLE001 - retry the idempotent copy
            last_error = exc
        logger.warning("attempt %d: promote %s -> %s failed (%s) — retrying", attempt, new, old, last_error)
    raise RuntimeError(f"could not promote {new} over {old}: {last_error}") from last_error


def _count_files(fs: s3fs.S3FileSystem, path: str) -> int:
    """Count files under *path*, retrying a silently-empty listing.

    On this store S3 listings can lag PUTs and return nothing; a zero count is
    therefore never trusted for a group that must exist.
    """
    for _ in range(12):
        n = len(fs.find(path))
        if n:
            return n
        time.sleep(5)
    raise RuntimeError(f"source listing {path!r} is empty after retries — refusing to swap")


def _wait_for_file_count(fs: s3fs.S3FileSystem, path: str, n: int) -> bool:
    """True once *path* lists at least *n* files (listing consistency retries)."""
    for _ in range(12):
        if len(fs.find(path)) >= n:
            return True
        time.sleep(5)
    return False


def _consolidate_verified(store: Any, group: str, expected_slots: int) -> None:
    """Consolidate metadata and verify the *group* is visible through it.

    Consolidation on this store can silently write metadata missing the
    just-promoted group (its listings lag PUTs). Retry until the consolidated
    root — what readers actually open — shows the group with the expected time
    axis length.
    """
    for attempt in range(6):
        datacube.consolidate(store)
        time.sleep(3)
        try:
            root = zarr.open_group(store, mode="r")
            if group in root and int(root[group]["time"].shape[0]) == expected_slots:
                return
        except Exception:  # noqa: BLE001 - retry consolidation
            pass
        logger.warning("attempt %d: %s missing from consolidated metadata — retrying consolidation", attempt, group)
    raise RuntimeError(f"consolidated metadata does not contain {group!r} after retries")
