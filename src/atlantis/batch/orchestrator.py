"""Dask LocalCluster orchestrator for batch granule processing.

Drives the full run loop:
  1. Build LocalCluster with adaptive workers.
  2. Filter inventory to only pending task IDs (crash-safe resume).
  3. Submit all futures at once via client.map().
  4. Drain results with as_completed(), writing to SQLite immediately.
  5. Log progress every cfg.log_every granules.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from loguru import logger

from atlantis.batch.config import BatchConfig
from atlantis.batch.tracker import get_pending, init_db, mark_done, mark_failed, stats


def _build_loguru_worker_plugin() -> Any:  # noqa: ANN401
    """Build a Dask ``WorkerPlugin`` that wires loguru inside each worker.

    Defined lazily so that ``atlantis.batch.orchestrator`` can still be
    imported when the optional ``[batch]`` extras (dask/distributed) are
    not installed.
    """
    from distributed.diagnostics.plugin import WorkerPlugin

    class _LoguruWorkerPlugin(WorkerPlugin):
        """Configure loguru inside each Dask worker process."""

        name = "loguru_setup"

        def setup(self, worker: Any) -> None:  # noqa: ANN401
            import sys

            from loguru import logger as wlogger

            wlogger.remove()
            wlogger.add(sys.stderr, level="INFO", enqueue=True)

    return _LoguruWorkerPlugin()


def run_batch(
    tasks: list[dict],
    process_fn: Callable[[dict], Any],
    cfg: BatchConfig,
) -> None:
    """Run *process_fn* over every item in *tasks* using a Dask LocalCluster.

    Args:
        tasks: List of task dicts. Each must have a ``"task_id"`` key.
        process_fn: Top-level (picklable) function that accepts one task dict
            and returns an object with ``.task_id`` and ``.output_uri``
            attributes on success, or raises on failure.
        cfg: Batch configuration (workers, memory, retries, db path, …).
    """
    from dask.distributed import Client, LocalCluster, as_completed, wait  # noqa: F401

    init_db(cfg.db_path)

    all_ids = {t["task_id"] for t in tasks}
    pending_ids = get_pending(cfg.db_path, all_ids)
    pending_tasks = [t for t in tasks if t["task_id"] in pending_ids]

    total = len(all_ids)
    already_done = total - len(pending_tasks)
    logger.info(
        "Batch start: {} total | {} already done | {} to process",
        total,
        already_done,
        len(pending_tasks),
    )

    if not pending_tasks:
        logger.info("Nothing to do — all tasks already marked DONE.")
        return

    cluster = LocalCluster(
        n_workers=cfg.workers_min,
        threads_per_worker=1,
        memory_limit=cfg.memory_limit_per_worker,
        dashboard_address=f":{cfg.dashboard_port}",
    )
    cluster.adapt(minimum=cfg.workers_min, maximum=cfg.workers_max)

    with Client(cluster) as client:
        client.register_plugin(_build_loguru_worker_plugin())
        logger.info("Dashboard: {}", client.dashboard_link)

        # Scatter first so client.map() references small future keys instead of
        # embedding every task dict as a literal graph argument (avoids the
        # "Sending large graph" warning for large catalogues).
        scattered = client.scatter(pending_tasks)
        futures = client.map(process_fn, scattered, retries=cfg.retries, pure=False)

        done_count = already_done
        fail_count = 0
        t_start = time.monotonic()

        for future in as_completed(futures):
            try:
                result = future.result()
                mark_done(cfg.db_path, result.task_id, result.output_uri)
                done_count += 1
            except Exception as exc:
                # Retrieve the task_id from the future's input key when available.
                task_id = getattr(future, "key", "unknown")
                err_msg = repr(exc)
                mark_failed(cfg.db_path, task_id, err_msg, attempts=cfg.retries + 1)
                fail_count += 1
                logger.warning("FAILED {}: {}", task_id, err_msg)

            if (done_count + fail_count - already_done) % cfg.log_every == 0:
                elapsed = time.monotonic() - t_start
                processed = done_count + fail_count - already_done
                rate = processed / elapsed * 3600 if elapsed > 0 else 0.0
                remaining = total - done_count - fail_count
                eta_h = remaining / (processed / elapsed) / 3600 if elapsed > 0 and processed > 0 else float("inf")
                logger.info(
                    "[{}/{}] {:.0f}% · {:.0f}/hr · ETA ~{:.1f}h · failures: {} · retries: {}",
                    done_count + fail_count,
                    total,
                    (done_count + fail_count) / total * 100,
                    rate,
                    eta_h,
                    fail_count,
                    cfg.retries,
                )

    final = stats(cfg.db_path)
    logger.info(
        "Batch complete: DONE={} FAILED={} total={}",
        final.get("DONE", 0),
        final.get("FAILED", 0),
        final.get("total", 0),
    )
