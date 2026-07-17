"""Archive writer for the consolidated Zarr flood datacube.

Harmonised rasters from every source are written into a single sharded Zarr
store, with one **group per source** co-registered on the canonical global
1-arcmin grid. Each ``(source, date)`` AOI is placed by an integer region write,
so the global grid stays sparse (only touched chunks materialise). Writes mutate
shared metadata (time axis, provenance registry) and must be driven by a single
coordinator. Local and S3 roots are both supported.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from atlantis.archive import datacube, grid
from atlantis.archive._store import store_for
from atlantis.config import ArchiveConfig
from atlantis.harmoniser.normaliser import Normaliser
from atlantis.models.event import FloodEvent

if TYPE_CHECKING:
    import xarray as xr

#: Spatial dimension candidates (checked in order).
_Y_DIMS = ("y", "lat", "latitude")
_X_DIMS = ("x", "lon", "longitude")

#: Data variables stored in the cube, in canonical order.
#: ``quality_mask`` / ``permanent_water`` are kept for backward compatibility —
#: :meth:`_ensure_masks` still synthesises them from ``water_fraction`` when a
#: caller passes ``ensure_masks=True``. ``cloud_mask``, ``snow_ice`` and
#: ``shadow`` are VIIRS-only derived masks (per-pixel 0/1); they materialise
#: when a source writes them (the per-session ``var_names`` is the actual
#: write-side filter).
_CUBE_VARS = (
    "water_fraction",
    "exclusion_mask",
    "reference_water",
    "quality_mask",
    "permanent_water",
    "cloud_mask",
    "snow_ice",
    "shadow",
    "recurring_flood",
)


def _find_dim(dataset: "xr.Dataset", candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if name in dataset.dims:
            return name
    return None


def _encode_uint8(values: np.ndarray) -> np.ndarray:
    """Encode a 2-D slice to uint8 storage.

    Float ``water_fraction`` in ``[0, 1]`` → ``[0, 100]`` (percent), ``NaN`` →
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

    def _store(self):
        return store_for(self.archive_root, self.config.store, self.storage_options)

    # ── Public API ─────────────────────────────────────────────────────────

    def write(
        self,
        dataset: "xr.Dataset",
        source_id: str,
        *,
        time: date | None = None,
        ensure_masks: bool = False,
        event: FloodEvent | None = None,
    ) -> Any:
        """Write harmonised data into the consolidated datacube.

        The AOI is region-written into the per-source group on the global grid,
        with inner chunks of ``config.chunk_size`` packed into ``config.shard_size``
        Zarr v3 shards. Only the channels present in the input are stored; pass
        ``ensure_masks=True`` to synthesise ``quality_mask`` / ``permanent_water``
        for a flood-fraction-only input.

        The daily pipeline writes label-free: provenance is recorded as bounded
        group attributes (``source_id``, ``last_updated``). Passing an optional
        ``event`` additionally registers a named **bookmark** (bbox + dates) under
        the ``atlantis_events`` group attr — for case studies / benchmarks only;
        omit it for routine daily ingestion so the schema does not grow.

        Writes mutate shared metadata (time-axis resize, provenance,
        consolidation) and must be driven by a single coordinator.

        Args:
            dataset: Harmonised xarray Dataset (``y``/``x`` aligned to the grid).
            source_id: Data source identifier / group name.
            time: Date for a 2-D (timeless) dataset. Required for a 2-D input
                unless ``event`` (whose ``start_date`` is then used) is given.
                Ignored if the dataset already has a ``time`` dimension.
            ensure_masks: Generate ``quality_mask`` / ``permanent_water`` if absent.
            event: Optional named event to register as a bookmark.

        Returns:
            The datacube store location (local :class:`~pathlib.Path` or
            :class:`zarr.storage.FsspecStore`).

        Raises:
            ValueError: If the dataset is empty, not grid-aligned, or 2-D without
                a ``time`` / ``event``.
        """
        if ensure_masks:
            dataset = self._ensure_masks(dataset)
        return self._write(dataset, source_id, time, event)

    def session(
        self,
        source_id: str,
        var_names: Sequence[str] = (
            "water_fraction",
            "exclusion_mask",
            "reference_water",
            "quality_mask",
            "permanent_water",
            "cloud_mask",
            "snow_ice",
            "shadow",
            "recurring_flood",
        ),
    ) -> _WriteSession:
        """Open a streaming write session over a single source group.

        Opens the store/group and acquires the resize handles **once**, then lets
        the caller stream many :meth:`_WriteSession.write` calls without
        re-opening or consolidating per call. Provenance is recorded and the
        store consolidated **once** on :meth:`_WriteSession.close` — use it as a
        context manager.

        This is the path the resume-safe cube batch uses: per-call consolidation
        would dominate S3 runtime, so it is deferred to the end of the run.
        Label-free only (no event bookmarks); like :meth:`write`, a session must
        be driven by a single coordinator.

        Args:
            source_id: Data source identifier / group name.
            var_names: Data variables the session will write (held in the group's
                canonical order). Channels absent from an individual ``write``
                input are skipped.

        Returns:
            A :class:`_WriteSession` bound to this writer.
        """
        return _WriteSession(self, source_id, list(var_names))

    # ── Internal write pipeline ────────────────────────────────────────────

    def _write(
        self,
        dataset: "xr.Dataset",
        source_id: str,
        time: date | None,
        event: FloodEvent | None,
    ) -> Any:
        if not dataset.data_vars:
            raise ValueError("Dataset is empty")

        ds = self._ensure_time_dim(dataset, time, event)
        var_names = [v for v in _CUBE_VARS if v in ds.data_vars] or list(ds.data_vars)
        store, group, time_arr, data_arrs = self._open_group_handles(source_id, var_names)

        times = self._write_regions(ds, var_names, time_arr, data_arrs)

        self._record_provenance(group, source_id)
        if event is not None:
            self._record_bookmark(group, event, times)
        datacube.consolidate(store)
        return store

    def _open_group_handles(self, source_id: str, var_names: list[str]):
        """Open the store, ensure the source group, and return write handles.

        Returns ``(store, group, time_arr, data_arrs)``. The array handles must be
        held for the lifetime of the writes — ``group[name]`` yields a fresh
        handle each call, so a resize on one would not be seen by the next (see
        :mod:`atlantis.archive.datacube`).
        """
        store = self._store()
        root = datacube.open_root(store, mode="a")
        group = datacube.ensure_source_group(
            root,
            source_id,
            var_names,
            chunk=self.config.chunk_size,
            shard=self.config.shard_size,
            scale_factor=self.config.scale_factor,
            time_units=datacube.epoch_units(self.config.time_epoch),
        )
        time_arr, data_arrs = datacube.get_handles(group, var_names)
        return store, group, time_arr, data_arrs

    def _write_regions(
        self,
        ds: "xr.Dataset",
        var_names: list[str],
        time_arr: Any,
        data_arrs: dict[str, Any],
    ) -> list[date]:
        """Region-write every ``(time, var)`` slice of ``ds`` into held handles.

        Returns the list of dates written. Does **not** consolidate or record
        provenance — callers do that once after a batch of region writes.
        """
        y_dim = _find_dim(ds, _Y_DIMS)
        x_dim = _find_dim(ds, _X_DIMS)
        if y_dim is None or x_dim is None:
            raise ValueError(
                f"Dataset must have recognisable spatial dimensions "
                f"(y: {_Y_DIMS}, x: {_X_DIMS}). Found dims: {list(ds.dims)}"
            )

        window = grid.coords_to_window(ds[y_dim].values, ds[x_dim].values)
        times = [self._as_date(t) for t in ds["time"].values]
        for ti, day in enumerate(times):
            t_int = datacube.date_to_int(day, self.config.time_epoch)
            time_idx = datacube.ensure_time_index(time_arr, data_arrs, t_int)
            for var in var_names:
                if var not in ds.data_vars:
                    continue
                arr = _encode_uint8(np.asarray(ds[var].isel(time=ti).values))
                datacube.write_region(data_arrs[var], time_idx, window, arr)
        return times

    def _ensure_masks(self, dataset: "xr.Dataset") -> "xr.Dataset":
        """Ensure ``quality_mask`` / ``permanent_water`` channels exist (generate if absent)."""
        normaliser = Normaliser()
        ds = dataset
        if "quality_mask" not in ds.data_vars:
            base = "water_fraction" if "water_fraction" in ds.data_vars else list(ds.data_vars)[0]
            ds = ds.assign(quality_mask=normaliser.generate_quality_mask(ds, variable=base))
        if "permanent_water" not in ds.data_vars:
            ds = ds.assign(permanent_water=normaliser.generate_permanent_water_mask(ds))
        return ds

    def _ensure_time_dim(self, dataset: "xr.Dataset", time: date | None, event: FloodEvent | None) -> "xr.Dataset":
        """Return a dataset with a ``time`` dimension/coordinate (datetime64)."""
        if "time" in dataset.dims:
            return dataset
        if time is not None:
            day = self._as_date(time)
        elif event is not None:
            day = event.start_date
        else:
            raise ValueError("A 2-D dataset requires `time` (a date) or an `event`.")
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
    def _record_provenance(group: Any, source_id: str) -> None:
        """Record bounded provenance in the source group's attributes."""
        group.attrs.update({"source_id": source_id, "last_updated": datetime.now(tz=timezone.utc).isoformat()})

    @staticmethod
    def _record_bookmark(group: Any, event: FloodEvent, times: list[date]) -> None:
        """Register an optional named event bookmark (bbox + dates).

        Stores only the AOI bbox and the set of dates, so the reader derives the
        index window from the bbox via the canonical grid. Bounded by the number
        of distinct named events — the daily pipeline never calls this.
        """
        registry = dict(group.attrs.get("atlantis_events", {}))
        existing = registry.get(event.event_id, {})
        dates = sorted(set(existing.get("dates", [])) | {d.isoformat() for d in times})
        registry[event.event_id] = {
            "bbox": list(event.bbox),
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


class _WriteSession:
    """A held-open streaming write session over one datacube source group.

    Created via :meth:`ArchiveWriter.session`. Holds the store and the resize
    handles so many slices can be region-written cheaply, then records provenance
    and consolidates **once** on :meth:`close`. Intended for label-free batch
    ingestion (e.g. the resume-safe cube batch), where per-call consolidation
    would dominate S3 runtime.
    """

    def __init__(self, writer: ArchiveWriter, source_id: str, var_names: list[str]) -> None:
        self._writer = writer
        self._source_id = source_id
        self._var_names = var_names
        self._store, self._group, self._time_arr, self._data_arrs = writer._open_group_handles(source_id, var_names)
        self._closed = False

    def write(self, dataset: "xr.Dataset", *, time: date | None = None) -> list[date]:
        """Region-write one dataset (2-D with ``time=`` or 3-D) into the cube.

        Returns the dates written. Does not consolidate — deferred to :meth:`close`.
        """
        ds = self._writer._ensure_time_dim(dataset, time, None)
        return self._writer._write_regions(ds, self._var_names, self._time_arr, self._data_arrs)

    def close(self) -> Any:
        """Record provenance and consolidate the store once. Idempotent."""
        if not self._closed:
            self._writer._record_provenance(self._group, self._source_id)
            datacube.consolidate(self._store)
            self._closed = True
        return self._store

    def __enter__(self) -> _WriteSession:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False
