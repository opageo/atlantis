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
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import numpy as np
import zarr

from atlantis.archive import grid

if TYPE_CHECKING:
    from atlantis.archive.grid import IndexWindow

#: Nodata sentinel shared with the harmonised GeoTIFF encoding.
NODATA: int = 255


def epoch_units(epoch: str) -> str:
    """CF time units string, e.g. ``"days since 2020-01-01"``."""
    return f"days since {epoch}"


def date_to_int(value: date | datetime | np.datetime64, epoch: str) -> int:
    """Convert a date to an integer number of days since *epoch*."""
    d = np.datetime64(value, "D")
    base = np.datetime64(epoch, "D")
    return int((d - base) / np.timedelta64(1, "D"))


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
) -> zarr.Group:
    """Return the per-source group, creating it on the global grid if absent.

    Args:
        root: Root group of the datacube store.
        source_id: Source identifier (group name).
        var_names: Data variables to create (uint8, fill ``255``).
        chunk: Spatial chunk size (pixels) for ``y`` and ``x``.
        shard: Spatial shard size (pixels), or ``None`` to disable sharding.
        scale_factor: CF ``scale_factor`` applied to ``flood_fraction``.
        time_units: CF time units string for the ``time`` coordinate.

    Returns:
        The per-source :class:`zarr.Group`.
    """
    if source_id in root:
        group = root[source_id]
        _ensure_data_arrays(group, var_names, chunk=chunk, shard=shard, scale_factor=scale_factor)
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
    group.attrs.update({"crs": grid.GLOBAL_CRS, "atlantis_events": {}})
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
        if name == "flood_fraction":
            attrs.update({"scale_factor": scale_factor, "add_offset": 0.0, "long_name": "flood fraction", "units": "1"})
        else:
            attrs["long_name"] = name.replace("_", " ")
        arr.attrs.update(attrs)


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
