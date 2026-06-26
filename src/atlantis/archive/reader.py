"""Archive reader for the consolidated Zarr flood datacube."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from atlantis.config import ArchiveConfig

from ._store import store_for

if TYPE_CHECKING:
    import xarray as xr


class ArchiveReader:
    """Reads flood data from the consolidated Zarr datacube.

    Events are looked up via the per-source group's ``atlantis_events``
    provenance registry, which records each event's AOI index window and dates.
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
        event_id: str,
        source_id: str,
        *,
        tiles: list[tuple[int, int]] | None = None,
    ) -> "xr.Dataset":
        """Read an event's slice from the datacube, optionally as discrete tiles.

        Args:
            event_id: Flood event identifier.
            source_id: Data source identifier (group).
            tiles: Optional ``(row, col)`` tile indices within the event window.
                Each selects a ``config.chunk_size`` square; results are stacked
                along a new ``tile`` dimension (edge tiles padded with NaN).
                ``None`` returns the full event window.

        Returns:
            Lazily-loaded xarray Dataset clipped to the event AOI and dates.

        Raises:
            FileNotFoundError: If the datacube or source group does not exist.
            KeyError: If the event is not present in the source group.
        """
        import xarray as xr

        ds = self._open_group(source_id)
        window = self._select_event(ds, event_id, source_id)
        if tiles is None:
            return window

        tile = self.config.chunk_size
        blocks: list[xr.Dataset] = []
        for i, (row, col) in enumerate(tiles):
            y0, x0 = row * tile, col * tile
            block = window.isel(y=slice(y0, y0 + tile), x=slice(x0, x0 + tile))
            pad_y = tile - block.sizes.get("y", 0)
            pad_x = tile - block.sizes.get("x", 0)
            if pad_y > 0 or pad_x > 0:
                block = block.pad(y=(0, max(pad_y, 0)), x=(0, max(pad_x, 0)), constant_values=np.nan)
            block = block.drop_vars(["y", "x"], errors="ignore")
            block = block.expand_dims(tile=[i]).assign_coords(tile_row=("tile", [row]), tile_col=("tile", [col]))
            blocks.append(block)
        return xr.concat(blocks, dim="tile")

    def list_events(self) -> list[str]:
        """List all event IDs recorded in the datacube."""
        events: set[str] = set()
        for source_id in self._group_names():
            events.update(self._group_attrs(source_id).get("atlantis_events", {}))
        return sorted(events)

    def list_sources(self, event_id: str) -> list[str]:
        """List source IDs that contain *event_id*."""
        sources: set[str] = set()
        for source_id in self._group_names():
            if event_id in self._group_attrs(source_id).get("atlantis_events", {}):
                sources.add(source_id)
        return sorted(sources)

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

    def _select_event(self, ds: "xr.Dataset", event_id: str, source_id: str) -> "xr.Dataset":
        registry = ds.attrs.get("atlantis_events", {})
        if event_id not in registry:
            raise KeyError(f"Event '{event_id}' not found in source '{source_id}'.")
        entry = registry[event_id]
        sub = ds.isel(
            y=slice(entry["row_start"], entry["row_stop"]),
            x=slice(entry["col_start"], entry["col_stop"]),
        )
        dates = np.array(entry.get("dates", []), dtype="datetime64[ns]")
        if dates.size and "time" in sub.dims:
            present = np.isin(sub["time"].values, dates)
            sub = sub.isel(time=np.flatnonzero(present))
        return sub

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
