"""Archive reader for the consolidated Zarr flood datacube."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from atlantis.archive import grid
from atlantis.config import ArchiveConfig

from ._store import store_for

if TYPE_CHECKING:
    import xarray as xr


def _to_dt64(value: date | str | None):
    """Coerce a date / ISO string to ``datetime64[ns]`` (or ``None``)."""
    if value is None:
        return None
    return np.datetime64(value, "ns")


class ArchiveReader:
    """Reads flood data from the consolidated Zarr datacube.

    The daily archive is queried by ``(source, time, space)`` — temporal
    selection on the ``time`` axis and spatial selection from a bbox (mapped to
    an index window via the canonical grid). Named **event bookmarks** are an
    optional convenience overlay (``atlantis_events`` group attr) for case
    studies / benchmarks; the daily pipeline never writes them.
    """

    def __init__(
        self,
        archive_root: str | Path,
        config: ArchiveConfig | None = None,
        *,
        storage_options: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the archive reader.

        Args:
            archive_root: Archive root — a local directory or an ``s3://`` URI.
            config: Archive configuration. Defaults to :class:`ArchiveConfig`.
            storage_options: fsspec options for remote roots.
        """
        self.archive_root = str(archive_root)
        self.config = config or ArchiveConfig()
        self.storage_options = storage_options if storage_options is not None else self.config.storage_options

    # ── Public API ─────────────────────────────────────────────────────────

    def read(
        self,
        source_id: str,
        *,
        bbox: tuple[float, float, float, float] | None = None,
        start: date | str | None = None,
        end: date | str | None = None,
        tiles: list[tuple[int, int]] | None = None,
        event: str | None = None,
    ) -> "xr.Dataset":
        """Read a source's flood data from the datacube.

        Primary (daily) access is by ``(source, time, space)``:

        * **temporal** — ``start`` / ``end`` select an inclusive date range on
          the ``time`` axis (``None`` = unbounded).
        * **spatial** — ``bbox`` ``(west, south, east, north)`` is mapped to an
          index window via the canonical grid (``None`` = full global grid).

        ``event`` resolves an optional named bookmark instead (its bbox + dates)
        and is mutually exclusive with ``bbox`` / ``start`` / ``end``.

        Args:
            source_id: Data source identifier (group).
            bbox: AOI bounds ``(west, south, east, north)`` in degrees.
            start: Inclusive start date (``date`` or ISO string).
            end: Inclusive end date (``date`` or ISO string).
            tiles: Optional ``(row, col)`` tile indices within the selected
                window; each selects a ``config.chunk_size`` square, stacked on a
                new ``tile`` dimension (edge tiles NaN-padded).
            event: Optional named event bookmark to resolve.

        Returns:
            Lazily-loaded, CF-decoded xarray Dataset.

        Raises:
            FileNotFoundError: If the datacube or source group does not exist.
            KeyError: If *event* is given but not registered.
        """
        ds = self._open_group(source_id)
        if event is not None:
            ds = self._select_bookmark(ds, event, source_id)
        else:
            if bbox is not None:
                win = grid.bounds_to_window(*bbox)
                ds = ds.isel(y=slice(win.row_start, win.row_stop), x=slice(win.col_start, win.col_stop))
            if start is not None or end is not None:
                ds = ds.sel(time=slice(_to_dt64(start), _to_dt64(end)))
        if tiles is None:
            return ds
        return self._stack_tiles(ds, tiles)

    def list_sources(self) -> list[str]:
        """List the source groups present in the datacube."""
        return self._group_names()

    def list_events(self) -> list[str]:
        """List the optional named event bookmarks recorded in the datacube."""
        events: set[str] = set()
        for source_id in self._group_names():
            events.update(self._group_attrs(source_id).get("atlantis_events", {}))
        return sorted(events)

    # ── Internal helpers ───────────────────────────────────────────────────

    def _store(self):
        return store_for(self.archive_root, self.config.store, self.storage_options)

    def _open_group(self, source_id: str) -> "xr.Dataset":
        import xarray as xr

        store = self._store()
        if isinstance(store, Path) and not store.exists():
            raise FileNotFoundError(f"Datacube not found: {store}")
        for consolidated in (True, False):
            try:
                return xr.open_zarr(store, group=source_id, consolidated=consolidated, decode_coords="all")
            except (FileNotFoundError, KeyError):
                break
            except Exception:
                continue
        raise FileNotFoundError(f"Source group '{source_id}' not found in {store}")

    def _select_bookmark(self, ds: "xr.Dataset", event_id: str, source_id: str) -> "xr.Dataset":
        registry = ds.attrs.get("atlantis_events", {})
        if event_id not in registry:
            raise KeyError(f"Event bookmark '{event_id}' not found in source '{source_id}'.")
        entry = registry[event_id]
        win = grid.bounds_to_window(*entry["bbox"])
        sub = ds.isel(
            y=slice(win.row_start, win.row_stop),
            x=slice(win.col_start, win.col_stop),
        )
        dates = np.array(entry.get("dates", []), dtype="datetime64[ns]")
        if dates.size and "time" in sub.dims:
            present = np.isin(sub["time"].values, dates)
            sub = sub.isel(time=np.flatnonzero(present))
        return sub

    def _stack_tiles(self, ds: "xr.Dataset", tiles: list[tuple[int, int]]) -> "xr.Dataset":
        import xarray as xr

        tile = self.config.chunk_size
        blocks: list[xr.Dataset] = []
        for i, (row, col) in enumerate(tiles):
            y0, x0 = row * tile, col * tile
            block = ds.isel(y=slice(y0, y0 + tile), x=slice(x0, x0 + tile))
            pad_y = tile - block.sizes.get("y", 0)
            pad_x = tile - block.sizes.get("x", 0)
            if pad_y > 0 or pad_x > 0:
                block = block.pad(y=(0, max(pad_y, 0)), x=(0, max(pad_x, 0)), constant_values=np.nan)
            block = block.drop_vars(["y", "x"], errors="ignore")
            block = block.expand_dims(tile=[i]).assign_coords(tile_row=("tile", [row]), tile_col=("tile", [col]))
            blocks.append(block)
        return xr.concat(blocks, dim="tile")

    def _group_names(self) -> list[str]:
        import zarr

        store = self._store()
        if isinstance(store, Path) and not store.exists():
            return []
        try:
            root = zarr.open_group(store, mode="r")
            return sorted(root.group_keys())
        except Exception:
            return []

    def _group_attrs(self, source_id: str) -> dict:
        import zarr

        store = self._store()
        try:
            root = zarr.open_group(store, mode="r")
            return dict(root[source_id].attrs)
        except Exception:
            return {}
