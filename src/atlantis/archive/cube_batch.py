"""Resume-safe, streaming batch that builds the Zarr datacube from a catalogue.

Unlike the COG batch (where each worker writes its own independent object), a
Zarr cube shares metadata across all writes, so it must be assembled by a single
coordinator. This module keeps the **expensive produce step parallel** (Dask
harmonises granules across workers) while **streaming** each result into the cube
through one writer session — bounded memory, no giant in-RAM accumulation.

Every finished ``(source, date)`` task is recorded in the SQLite tracker, so a
run is crash-/disconnect-safe: re-running skips work already marked ``DONE`` and
only the still-``PENDING``/``FAILED`` tasks are reprocessed. Run it detached
(``tmux`` / ``nohup``) so an SSH ``SIGHUP`` cannot kill the coordinator, and
check progress offline with :func:`atlantis.batch.tracker.stats` (exposed as
``atlantis batch viirs cube status``).
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from loguru import logger

from atlantis.batch.config import BatchConfig
from atlantis.batch.tracker import get_pending, init_db, mark_done, mark_failed, stats


def run_cube_batch(
    tasks: list[dict[str, Any]],
    produce_fn: Callable[[dict[str, Any]], dict[str, Any]],
    consume: Callable[[dict[str, Any]], str],
    cfg: BatchConfig,
) -> dict[str, Any]:
    """Stream Dask-produced payloads into a single consumer, tracked in SQLite.

    The produce/consume split keeps the cube write serial (one coordinator) while
    the heavy lifting runs in parallel:

    * ``produce_fn`` runs **on Dask workers** — it must be picklable and
      self-contained (e.g. download + harmonise a granule), returning a payload
      dict that includes a ``"task_id"`` key.
    * ``consume`` runs **on the coordinator** — it receives each payload as soon
      as it is ready (via ``as_completed``) and writes it into the cube,
      returning an output URI marker. It may close over unpicklable state (an
      open writer session).

    Each payload is marked ``DONE`` only after ``consume`` returns, so a task
    killed mid-write is left ``PENDING`` and safely redone on resume (region
    writes are idempotent). Already-``DONE`` tasks are skipped up front.

    Args:
        tasks: Task dicts, each with a unique ``"task_id"``.
        produce_fn: Picklable worker function ``task -> payload``.
        consume: Coordinator-side ``payload -> output_uri``.
        cfg: Batch configuration (tracker path, worker bounds, retries, logging).

    Returns:
        Final tracker :func:`~atlantis.batch.tracker.stats` for the run.
    """
    from dask.distributed import Client, LocalCluster, as_completed

    init_db(cfg.db_path)
    all_ids = {t["task_id"] for t in tasks}
    pending_ids = get_pending(cfg.db_path, all_ids)
    pending = [t for t in tasks if t["task_id"] in pending_ids]
    total = len(all_ids)
    already = total - len(pending)
    logger.info("Cube batch: {} total | {} done | {} pending", total, already, len(pending))
    if not pending:
        logger.info("Nothing to do — all tasks already DONE.")
        return stats(cfg.db_path)

    cluster = LocalCluster(
        n_workers=cfg.workers_min,
        threads_per_worker=1,
        memory_limit=cfg.memory_limit_per_worker,
        dashboard_address=f":{cfg.dashboard_port}",
    )
    cluster.adapt(minimum=cfg.workers_min, maximum=cfg.workers_max)

    done = 0
    failed = 0
    start = _time.monotonic()
    try:
        with Client(cluster) as client:
            logger.info("Dask dashboard: {}", client.dashboard_link)
            # Scatter the task dicts to workers once up front instead of letting client.map()
            # embed each dict as a literal graph argument — for 100k+ tasks that literal
            # embedding is what triggers Dask's "Sending large graph" warning and the
            # associated (de)serialization overhead on every submission.
            scattered = client.scatter(pending)
            futures = client.map(produce_fn, scattered, retries=cfg.retries, pure=False)
            key_to_id = {future.key: task["task_id"] for future, task in zip(futures, pending)}
            for future in as_completed(futures):
                task_id = key_to_id.get(future.key, "unknown")
                try:
                    payload = future.result()
                    uri = consume(payload)
                    mark_done(cfg.db_path, payload["task_id"], uri)
                    done += 1
                except Exception as exc:  # noqa: BLE001 - record and continue
                    mark_failed(cfg.db_path, task_id, repr(exc), attempts=cfg.retries + 1)
                    failed += 1
                    logger.warning("FAILED {}: {}", task_id, exc)
                finally:
                    future.release()

                processed = done + failed
                if processed % cfg.log_every == 0:
                    elapsed = _time.monotonic() - start
                    rate = processed / elapsed * 3600 if elapsed > 0 else 0.0
                    logger.info(
                        "[{}/{}] {:.1f}% · {:.0f}/hr · remaining {} · failures {}",
                        already + processed,
                        total,
                        100 * (already + processed) / total,
                        rate,
                        len(pending) - processed,
                        failed,
                    )
    finally:
        cluster.close()

    final = stats(cfg.db_path)
    logger.info(
        "Cube batch complete: DONE={} FAILED={} total={}",
        final.get("DONE", 0),
        final.get("FAILED", 0),
        final.get("total", 0),
    )
    return final


def run_viirs_cube_batch(
    tasks: list[dict[str, Any]],
    *,
    archive_root: str,
    cfg: BatchConfig,
    archive_config: Any = None,
    storage_options: dict[str, Any] | None = None,
    source_id: str = "viirs",
) -> dict[str, Any]:
    """Build the VIIRS datacube from a catalogue, resume-safe and streaming.

    Wires the VIIRS produce step (:func:`harmonise_granule_payload`, run on Dask
    workers) to a single held-open :meth:`~atlantis.archive.writer.ArchiveWriter.session`
    on the coordinator: each harmonised granule is region-written into the
    ``flood_fraction`` channel as it arrives, and the store is consolidated once
    when the session closes.

    Args:
        tasks: VIIRS task dicts (from
            :func:`atlantis.fetchers.viirs.inventory.to_tasks`).
        archive_root: Cube root — a local path or an ``s3://`` URI.
        cfg: Batch configuration.
        archive_config: Optional :class:`~atlantis.config.ArchiveConfig`.
        storage_options: fsspec options for a remote ``archive_root``.
        source_id: Cube group name (default ``"viirs"``).

    Returns:
        Final tracker stats for the run.
    """
    from atlantis.archive.writer import ArchiveWriter
    from atlantis.fetchers.viirs.batch_processor import harmonise_granule_payload

    writer = ArchiveWriter(archive_root, archive_config, storage_options=storage_options)
    with writer.session(source_id, ("flood_fraction",)) as session:

        def consume(payload: dict[str, Any]) -> str:
            session.write(_payload_to_dataset(payload), time=_to_date(payload["date"]))
            return f"{archive_root}#{source_id}/{payload['date']}/aoi{int(payload['aoi_id']):03d}"

        return run_cube_batch(tasks, harmonise_granule_payload, consume, cfg)


def _payload_to_dataset(payload: dict[str, Any]):
    """Build a 2-D ``flood_fraction`` dataset from a harmonised granule payload."""
    import xarray as xr

    return xr.Dataset(
        {"flood_fraction": (("y", "x"), payload["scaled"])},
        coords={"y": payload["y"], "x": payload["x"]},
    )


def _to_date(value: Any) -> date:
    """Coerce a date / datetime / datetime64 / ISO string to ``datetime.date``."""
    if isinstance(value, str):
        return date.fromisoformat(value)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    import pandas as pd

    ts = pd.Timestamp(value)
    return date(int(ts.year), int(ts.month), int(ts.day))
