"""Archive writer for the consolidated Zarr flood datacube.

Harmonised rasters from every source are written into a single Zarr store per
layer (analysis-ready ``raw`` and ``ml-ready``), with one **group per source**
co-registered on the canonical global 1-arcmin grid. Each ``(source, date)``
AOI is placed by an integer region write, so the global grid stays sparse
(only touched chunks materialise) and parallel writes to disjoint dates/regions
never collide. Local and S3 roots are both supported.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from atlantis.archive import datacube, grid
from atlantis.archive._store import store_for
from atlantis.config import ArchiveConfig, HarmoniseConfig
from atlantis.harmoniser.normaliser import Normaliser
from atlantis.models.event import FloodEvent

if TYPE_CHECKING:
    import xarray as xr

#: Spatial dimension candidates (checked in order).
_Y_DIMS = ("y", "lat", "latitude")
_X_DIMS = ("x", "lon", "longitude")

#: Data variables stored in the cube, in canonical order.
_CUBE_VARS = ("flood_fraction", "quality_mask", "permanent_water", "recurring_flood")


def _find_dim(dataset: "xr.Dataset", candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if name in dataset.dims:
            return name
    return None


def _encode_uint8(values: np.ndarray) -> np.ndarray:
    """Encode a 2-D slice to uint8 storage.

    Float ``flood_fraction`` in ``[0, 1]`` → ``[0, 100]`` (percent), ``NaN`` →
    ``255`` nodata. Integer masks / already-percent uint8 inputs pass through.
    Mirrors :func:`atlantis.harmoniser.write_harmonised_raster`.
    """
    if np.issubdtype(values.dtype, np.floating):
        return np.where(np.isnan(values), datacube.NODATA, np.clip(np.round(values * 100), 0, 100)).astype("uint8")
    return values.astype("uint8")


class ArchiveWriter:
    """Writes harmonised flood data into the consolidated Zarr datacube."""

    def __init__(
        self,
        archive_root: str | Path,
        config: ArchiveConfig | None = None,
        *,
        storage_options: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the archive writer.

        Args:
            archive_root: Archive root — a local directory or an ``s3://`` URI.
            config: Archive configuration (store names, chunk/shard sizes, CF
                scale, time epoch). Defaults to :class:`ArchiveConfig`.
            storage_options: fsspec options for remote roots. Falls back to
                ``config.storage_options``.
        """
        self.archive_root = str(archive_root)
        self.config = config or ArchiveConfig()
        self.storage_options = storage_options if storage_options is not None else self.config.storage_options

    # ── Store helpers ──────────────────────────────────────────────────────

    def _store(self, layer: str):
        name = self.config.raw_store if layer == "raw" else self.config.ml_store
        return store_for(self.archive_root, name, self.storage_options)

    # ── Public API ─────────────────────────────────────────────────────────

    def write_raw(
        self,
        dataset: "xr.Dataset",
        event: FloodEvent,
        source_id: str,
        time: date | None = None,
    ) -> Any:
        """Write harmonised data into the analysis-ready datacube.

        The dataset's AOI is region-written into the per-source group on the
        global grid (spatial chunks of ``config.raw_chunk_size``, unsharded so
        parallel backfills stay chunk-disjoint and lock-free).

        Args:
            dataset: Harmonised xarray Dataset (``y``/``x`` aligned to the grid).
            event: Flood event metadata (provenance + reader lookup).
            source_id: Data source identifier / group name.
            time: Date for a 2-D (timeless) dataset. Defaults to
                ``event.start_date``. Ignored if the dataset already has ``time``.

        Returns:
            The datacube store location (local :class:`~pathlib.Path` or
            :class:`zarr.storage.FsspecStore`).

        Raises:
            ValueError: If the dataset is empty or not grid-aligned.
        """
        return self._write(dataset, event, source_id, time, layer="raw")

    def write_ml_ready(
        self,
        dataset: "xr.Dataset",
        event: FloodEvent,
        source_id: str,
        harmonise_config: HarmoniseConfig | None = None,
        time: date | None = None,
    ) -> Any:
        """Write normalised, masked data into the ML-ready datacube.

        Spatial chunks equal ``config.ml_tile_size`` (the data-loader read
        granularity) packed into ``config.ml_shard_size`` Zarr v3 shards (the
        S3 object granularity) — fine-grained random tile reads with few large
        objects. Because shards bundle many inner chunks into one object, the
        ML cube is written by a single coordinator (not parallel workers).

        Args:
            dataset: Harmonised xarray Dataset.
            event: Flood event metadata.
            source_id: Data source identifier / group name.
            harmonise_config: Optional harmonisation config (reserved).
            time: Date for a 2-D dataset (defaults to ``event.start_date``).

        Returns:
            The ML datacube store location.
        """
        prepared = self._prepare_ml(dataset)
        return self._write(prepared, event, source_id, time, layer="ml")

    # ── Internal write pipeline ────────────────────────────────────────────

    def _write(
        self,
        dataset: "xr.Dataset",
        event: FloodEvent,
        source_id: str,
        time: date | None,
        layer: str,
    ) -> Any:
        if not dataset.data_vars:
            raise ValueError("Dataset is empty")

        ds = self._ensure_time_dim(dataset, event, time)
        y_dim = _find_dim(ds, _Y_DIMS)
        x_dim = _find_dim(ds, _X_DIMS)
        if y_dim is None or x_dim is None:
            raise ValueError(
                f"Dataset must have recognisable spatial dimensions "
                f"(y: {_Y_DIMS}, x: {_X_DIMS}). Found dims: {list(ds.dims)}"
            )

        window = grid.coords_to_window(ds[y_dim].values, ds[x_dim].values)
        var_names = [v for v in _CUBE_VARS if v in ds.data_vars] or list(ds.data_vars)

        store = self._store(layer)
        if layer == "raw":
            chunk, shard = self.config.raw_chunk_size, None
        else:
            chunk, shard = self.config.ml_tile_size, self.config.ml_shard_size

        root = datacube.open_root(store, mode="a")
        group = datacube.ensure_source_group(
            root,
            source_id,
            var_names,
            chunk=chunk,
            shard=shard,
            scale_factor=self.config.scale_factor,
            time_units=datacube.epoch_units(self.config.time_epoch),
        )

        times = [self._as_date(t) for t in ds["time"].values]
        time_arr, data_arrs = datacube.get_handles(group, var_names)
        for ti, day in enumerate(times):
            t_int = datacube.date_to_int(day, self.config.time_epoch)
            time_idx = datacube.ensure_time_index(time_arr, data_arrs, t_int)
            for var in var_names:
                arr = _encode_uint8(np.asarray(ds[var].isel(time=ti).values))
                datacube.write_region(data_arrs[var], time_idx, window, arr)

        self._record_event(group, event, window, times)
        datacube.consolidate(store)
        return store

    def _prepare_ml(self, dataset: "xr.Dataset") -> "xr.Dataset":
        """Normalise the flood variable and ensure quality / permanent-water masks."""
        normaliser = Normaliser()
        ds = dataset
        if "flood_fraction" in ds.data_vars:
            ds = normaliser.normalise(ds, variable="flood_fraction")
        if "quality_mask" not in ds.data_vars:
            base = "flood_fraction" if "flood_fraction" in ds.data_vars else list(ds.data_vars)[0]
            ds = ds.assign(quality_mask=normaliser.generate_quality_mask(ds, variable=base))
        if "permanent_water" not in ds.data_vars:
            ds = ds.assign(permanent_water=normaliser.generate_permanent_water_mask(ds))
        return ds

    def _ensure_time_dim(self, dataset: "xr.Dataset", event: FloodEvent, time: date | None) -> "xr.Dataset":
        """Return a dataset with a ``time`` dimension/coordinate (datetime64)."""
        if "time" in dataset.dims:
            return dataset
        day = self._as_date(time) if time is not None else event.start_date
        return dataset.expand_dims(time=[np.datetime64(day, "ns")])

    @staticmethod
    def _as_date(value: Any) -> date:
        """Coerce date / datetime / datetime64 / ISO string to ``datetime.date``."""
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return np.datetime64(value, "D").astype(object)

    @staticmethod
    def _record_event(
        group: Any,
        event: FloodEvent,
        window: "grid.IndexWindow",
        times: list[date],
    ) -> None:
        """Record per-event provenance in the source group's attributes."""
        registry = dict(group.attrs.get("atlantis_events", {}))
        existing = registry.get(event.event_id, {})
        dates = sorted(set(existing.get("dates", [])) | {d.isoformat() for d in times})
        registry[event.event_id] = {
            "bbox": list(event.bbox),
            "row_start": window.row_start,
            "row_stop": window.row_stop,
            "col_start": window.col_start,
            "col_stop": window.col_stop,
            "dates": dates,
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        group.attrs["atlantis_events"] = registry

    def write_checkpoint(
        self,
        event: FloodEvent,
        source_id: str,
        stage: str,
    ) -> Path:
        """Write a checkpoint marker for resumed processing.

        Args:
            event: Flood event metadata.
            source_id: Data source identifier.
            stage: Processing stage (fetch, harmonise, archive).

        Returns:
            Path to checkpoint marker file.
        """
        checkpoint_dir = Path(self.archive_root) / ".checkpoints" / event.event_id
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = checkpoint_dir / f"{source_id}_{stage}.done"
        checkpoint_path.touch()
        return checkpoint_path

    def is_checkpointed(
        self,
        event: FloodEvent,
        source_id: str,
        stage: str,
    ) -> bool:
        """Check if a processing stage is checkpointed.

        Args:
            event: Flood event metadata.
            source_id: Data source identifier.
            stage: Processing stage.

        Returns:
            True if checkpoint exists, False otherwise.
        """
        checkpoint_path = Path(self.archive_root) / ".checkpoints" / event.event_id / f"{source_id}_{stage}.done"
        return checkpoint_path.exists()
