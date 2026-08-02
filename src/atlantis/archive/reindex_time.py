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
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import zarr

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

    Returns:
        The new sorted time values (int days since epoch).

    Raises:
        ValueError: If the group does not exist in the store.
    """
    root = zarr.open_group(store, mode="a")
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

    # Old time index i lands at new position perm[i].
    perm = np.searchsorted(target, times)
    tmp_name = f"_{source_id}{_TEMP_SUFFIX}"
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

    _swap_group(store, source_id, tmp_name)
    datacube.consolidate(store)
    return target


def _swap_group(store: Any, old: str, new: str) -> None:
    """Replace group *old* with group *new* (which is renamed to *old*)."""
    if isinstance(store, Path):
        old_dir, new_dir = store / old, store / new
        shutil.rmtree(old_dir)
        new_dir.rename(old_dir)
        return
    fs, base = store.fs, store.path
    fs.rm(f"{base}/{old}", recursive=True)
    fs.mv(f"{base}/{new}", f"{base}/{old}")
