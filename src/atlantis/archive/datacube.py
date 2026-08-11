"""Low-level Zarr v3 operations for the consolidated flood datacube.

The datacube is a single Zarr store with one **group per source** (``gfm``,
``modis``, ``viirs``, ...). Every group shares the canonical global 1-arcmin
grid (``time``, ``y``, ``x``) defined in :mod:`atlantis.archive.grid` and is
written sparsely: only chunks overlapping an event AOI ever materialise.

Writes are **region writes** — each ``(source, date)`` slot is addressed by an
integer :class:`~atlantis.archive.grid.IndexWindow` and a time index, so
concurrent workers touching disjoint dates/regions never collide. Extending the
``time`` axis is a metadata operation and must be performed by a single
coordinator (the archive CLI runs single-process, satisfying this).

**Config-identity guarantee**: ``chunk``/``shard``/``scale_factor``/``time_epoch``
are fixed for a group the first time it is written and recorded in its
``archive_config`` attr; :func:`ensure_source_group` raises
:class:`ConfigMismatchError` on any later drift (see ``docs/archive/zarr-spec.md``).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np
import zarr
from loguru import logger

from atlantis.archive import grid

if TYPE_CHECKING:
    from atlantis.archive.grid import IndexWindow

#: Nodata sentinel shared with the harmonised GeoTIFF encoding.
NODATA: int = 255

#: Bounded group attr recording the config values baked into a group at creation.
_CONFIG_ATTR = "archive_config"


class ConfigMismatchError(ValueError):
    """Raised when a group's recorded archive config differs from the caller's.

    ``chunk``/``shard`` are fixed into each array's storage layout at creation,
    and ``scale_factor``/``time_units`` are baked into every value already
    encoded under them — a later write with different values would silently
    misencode or misalign data rather than fail loudly. See the "config-identity
    guarantee" in ``docs/archive/zarr-spec.md``.
    """


def _config_fingerprint(chunk: int, shard: int | None, scale_factor: float, time_units: str) -> dict[str, Any]:
    """Bounded fingerprint of the config values a group's arrays are built from."""
    return {"chunk_size": chunk, "shard_size": shard, "scale_factor": scale_factor, "time_units": time_units}


def _verify_config_fingerprint(group: zarr.Group, source_id: str, fingerprint: dict[str, Any]) -> None:
    """Enforce the config-identity guarantee against an existing group.

    A group created before this guard shipped has no recorded fingerprint —
    adopt the caller's config as the established baseline once instead of
    failing on archives that predate this check.
    """
    recorded = group.attrs.get(_CONFIG_ATTR)
    if recorded is None:
        group.attrs[_CONFIG_ATTR] = fingerprint
        return
    if dict(recorded) != fingerprint:
        raise ConfigMismatchError(
            f"ArchiveConfig drift for group {source_id!r}: recorded {recorded} but caller passed "
            f"{fingerprint}. Changing chunk/shard size, scale_factor, or time_epoch against an "
            "already-written archive silently misencodes or misaligns data — see the "
            "'config-identity guarantee' in docs/archive/zarr-spec.md."
        )


def epoch_units(epoch: str) -> str:
    """CF time units string, e.g. ``"days since 2020-01-01"``."""
    return f"days since {epoch}"


def date_to_int(value: date | datetime | np.datetime64, epoch: str) -> int:
    """Convert a date to an integer number of days since *epoch*."""
    d = np.datetime64(value, "D")
    base = np.datetime64(epoch, "D")
    return int((d - base) / np.timedelta64(1, "D"))


def decode_axis_dates(group: zarr.Group) -> list[date]:
    """Decode a group's ``time`` axis into :class:`datetime.date` (CF units).

    The single shared int→date decode for the cube's ``time`` axis — used by
    the update flow and the CLI's post-run validation so the epoch handling
    cannot drift from :func:`epoch_units` / :func:`date_to_int`. Order is
    preserved (not sorted); callers that need a sorted axis sort themselves.
    """
    times = np.asarray(group["time"][:], dtype="int64")
    units = group["time"].attrs.get("units", epoch_units("2020-01-01"))
    epoch = date.fromisoformat(str(units).rsplit("since ", 1)[-1].strip())
    return [epoch + timedelta(days=int(t)) for t in times]


def _crs_grid_mapping_attrs() -> dict[str, Any]:
    """CF grid-mapping attributes for the canonical CRS.

    Produces the attribute set ``pyproj`` / ``rioxarray`` understand so the
    ``grid_mapping="crs"`` reference on each data variable resolves to a real
    CRS (e.g. ``ds.rio.crs`` after ``open_zarr(..., decode_coords="all")``).
    """
    from pyproj import CRS

    attrs: dict[str, Any] = dict(CRS.from_user_input(grid.GLOBAL_CRS).to_cf())
    # GDAL / rioxarray also read the WKT from ``spatial_ref``.
    attrs["spatial_ref"] = attrs.get("crs_wkt", "")
    return attrs


def open_root(store: Any, mode: str = "a") -> zarr.Group:
    """Open (or create) the root group of a datacube store."""
    return zarr.open_group(store, mode=mode)


def ensure_source_group(
    root: zarr.Group,
    source_id: str,
    var_names: list[str],
    *,
    chunk: int,
    shard: int | None,
    scale_factor: float,
    time_units: str,
    prefill_year: int | None = None,
) -> zarr.Group:
    """Return the per-source group, creating it on the global grid if absent.

    Args:
        root: Root group of the datacube store.
        source_id: Source identifier (group name).
        var_names: Data variables to create (uint8, fill ``255``).
        chunk: Spatial chunk size (pixels) for ``y`` and ``x``.
        shard: Spatial shard size (pixels), or ``None`` to disable sharding.
        scale_factor: CF ``scale_factor`` applied to ``water_fraction``.
        time_units: CF time units string for the ``time`` coordinate.
        prefill_year: If set, pre-fill the ``time`` axis with the full
            calendar year (exactly 365 or 366 metadata-only slots) right after
            the data arrays are ensured, so any event date lands in a
            pre-existing slot and writes never move the axis.

    Returns:
        The per-source :class:`zarr.Group`.

    Raises:
        ConfigMismatchError: If an existing group's recorded config
            (chunk/shard/scale/epoch) differs from the values passed here.
    """
    fingerprint = _config_fingerprint(chunk, shard, scale_factor, time_units)
    if source_id in root:
        group = root[source_id]
        _verify_config_fingerprint(group, source_id, fingerprint)
        _ensure_data_arrays(group, var_names, chunk=chunk, shard=shard, scale_factor=scale_factor)
        if prefill_year is not None:
            prefill_year_axis(group, prefill_year, time_units)
        return group

    group = root.create_group(source_id)
    height, width = grid.GLOBAL_HEIGHT, grid.GLOBAL_WIDTH

    y = group.create_array(name="y", shape=(height,), chunks=(height,), dtype="float64", dimension_names=("y",))
    y[:] = grid.global_y_coords()
    y.attrs.update({"standard_name": "latitude", "units": "degrees_north", "axis": "Y"})

    x = group.create_array(name="x", shape=(width,), chunks=(width,), dtype="float64", dimension_names=("x",))
    x[:] = grid.global_x_coords()
    x.attrs.update({"standard_name": "longitude", "units": "degrees_east", "axis": "X"})

    t = group.create_array(name="time", shape=(0,), chunks=(512,), dtype="int64", dimension_names=("time",))
    t.attrs.update({"standard_name": "time", "units": time_units, "calendar": "proleptic_gregorian"})

    # Real CF grid-mapping variable so ``grid_mapping="crs"`` resolves to a CRS.
    crs = group.create_array(name="crs", shape=(), dtype="int64")
    crs[...] = 0
    crs.attrs.update(_crs_grid_mapping_attrs())

    _ensure_data_arrays(group, var_names, chunk=chunk, shard=shard, scale_factor=scale_factor)
    if prefill_year is not None:
        prefill_year_axis(group, prefill_year, time_units)
    group.attrs.update({"crs": grid.GLOBAL_CRS, "atlantis_events": {}, _CONFIG_ATTR: fingerprint})
    return group


def _ensure_data_arrays(
    group: zarr.Group,
    var_names: list[str],
    *,
    chunk: int,
    shard: int | None,
    scale_factor: float,
) -> None:
    """Create any missing uint8 data arrays on the global grid (time-aligned)."""
    height, width = grid.GLOBAL_HEIGHT, grid.GLOBAL_WIDTH
    n_time = int(group["time"].shape[0]) if "time" in group else 0
    chunks3 = (1, chunk, chunk)
    shards3 = (1, shard, shard) if shard else None
    for name in var_names:
        if name in group:
            continue
        arr = group.create_array(
            name=name,
            shape=(n_time, height, width),
            chunks=chunks3,
            shards=shards3,
            dtype="uint8",
            fill_value=NODATA,
            dimension_names=("time", "y", "x"),
        )
        attrs: dict[str, Any] = {"_FillValue": NODATA, "grid_mapping": "crs"}
        if name == "water_fraction":
            attrs.update({"scale_factor": scale_factor, "add_offset": 0.0, "long_name": "water fraction", "units": "1"})
        else:
            attrs["long_name"] = name.replace("_", " ")
        arr.attrs.update(attrs)


def prefill_year_axis(group: zarr.Group, year: int, time_units: str) -> bool:
    """Pre-fill the ``time`` axis with every day of *year* (metadata-only).

    Resizes the time axis and every time-major data array to the exact number
    of days in *year* (365, or 366 in leap years) and writes the day integers,
    so any event date lands in a pre-existing slot: writes never move the axis
    and no reindex is ever needed. Existing chunk data is untouched (a V3
    resize is shape/metadata only) — only use it on empty groups (the scaffold,
    ``time`` shape 0) or fresh creations; pre-filling a group with
    already-written data would leave that data at its old index, misaligned
    with the new time values. Unwritten slots read NODATA.

    On an actual resize the group marker attribute ``atlantis_time_prefill`` is
    set to the year — downstream code (the MODIS update flow, CLI validation)
    keys off it to tell prefilled axes from data-proven ones. The marker is
    written **before** the resizes so an interrupted prefill leaves a marker
    without a full axis (which downstream treats as a loud failure) instead of
    a full axis without a marker (which it would silently treat as
    data-proven). The marker is only written on a resize, so legacy prefilled
    groups (no marker) remain detectable as non-prefilled; legacy 366-slot
    groups for 365-day years are left as-is (idempotent no-op; the stray slot
    is harmless).

    Idempotent: an axis already at the full day count (or beyond) is left
    as-is. A group with ``0 < n < days`` slots has partial data and is skipped
    with a warning — pre-filling it would leave that data at its old index,
    misaligned with the new time values.

    Returns:
        True if the axis was resized (metadata changed); False for a no-op.
    """
    days = (date(year + 1, 1, 1) - date(year, 1, 1)).days
    time_arr = group["time"]
    n = int(time_arr.shape[0])
    if n >= days:
        return False
    if n > 0:
        logger.warning(
            "skipping time-axis prefill for year {}: group already has {} of {} slots filled; "
            "pre-filling a partially-written group would misalign existing data",
            year,
            n,
            days,
        )
        return False
    group.attrs["atlantis_time_prefill"] = str(year)
    epoch = str(time_units).rsplit("since ", 1)[-1].strip()
    day_ints = np.asarray(
        [date_to_int(date(year, 1, 1) + timedelta(days=i), epoch) for i in range(days)],
        dtype="int64",
    )
    time_arr.resize((days,))
    time_arr[:] = day_ints
    for name in group.array_keys():
        arr = group[name]
        if len(arr.shape) == 3 and arr.shape[0] == n:
            arr.resize((days, arr.shape[1], arr.shape[2]))
    return True


def get_handles(group: zarr.Group, var_names: list[str]) -> tuple[Any, dict[str, Any]]:
    """Fetch stable array handles for the time axis and data variables.

    Zarr re-reads ``group[name]`` from cached group metadata on each access, so
    a resize on one fetched handle is not seen by a later fetch. Holding the
    handles and resizing/writing them in place keeps shapes consistent within a
    write.
    """
    return group["time"], {name: group[name] for name in var_names}


def ensure_time_index(time_arr: Any, data_arrs: dict[str, Any], t_int: int) -> int:
    """Return the time index for *t_int*, appending a new slot if needed.

    Operates on held handles (see :func:`get_handles`). Extending the ``time``
    axis resizes the time-major data arrays in place — a cheap metadata update;
    no chunk data is written. Single-coordinator only.
    """
    n = int(time_arr.shape[0])
    if n > 0:
        hits = np.where(time_arr[:] == t_int)[0]
        if hits.size:
            return int(hits[0])

    time_arr.resize((n + 1,))
    time_arr[n] = t_int
    for arr in data_arrs.values():
        if arr.shape[0] == n:
            arr.resize((n + 1, arr.shape[1], arr.shape[2]))
    return n


def write_region(arr: Any, time_idx: int, window: "IndexWindow", data: np.ndarray) -> None:
    """Write a uint8 AOI block into *arr* at *time_idx* / *window*."""
    arr[
        time_idx,
        window.row_start : window.row_stop,
        window.col_start : window.col_stop,
    ] = data


def consolidate(store: Any) -> None:
    """Consolidate metadata for the datacube store (best-effort)."""
    try:
        zarr.consolidate_metadata(store)
    except Exception:  # pragma: no cover - consolidation is an optimisation only
        pass
