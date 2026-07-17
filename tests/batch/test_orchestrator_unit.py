"""Fast unit tests for atlantis.batch.orchestrator with mocked Dask.

The existing ``test_orchestrator.py`` file contains ``@pytest.mark.slow``
integration tests that spin up a real Dask ``LocalCluster``. These tests
mock the ``dask.distributed`` imports so the full ``run_batch`` control
flow — resume filtering, success/failure handling, progress logging, and
the "nothing to do" early return — can be exercised quickly and
deterministically without any Dask dependency.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from atlantis.batch import BatchConfig, TaskResult, run_batch
from atlantis.batch.tracker import init_db, mark_done, stats

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _succeed(task: dict) -> TaskResult:
    return TaskResult(task_id=task["task_id"], output_uri=f"s3://atlantis/{task['task_id']}.tif")


def _fail(task: dict) -> TaskResult:
    raise RuntimeError(f"simulated failure for {task['task_id']}")


def _make_tasks(n: int) -> list[dict]:
    return [{"task_id": f"task_{i:04d}", "source_uri": f"s3://test/{i}", "dest_key": f"test/{i}.tif"} for i in range(n)]


class _FakeFuture:
    """A minimal stand-in for a Dask future.

    ``run_batch`` calls ``.result()`` on each future yielded by
    ``as_completed`` and, on failure, reads ``getattr(future, "key", ...)``.
    """

    def __init__(self, task_id: str, result: TaskResult | None, exc: Exception | None = None):
        self.key = task_id
        self._result = result
        self._exc = exc

    def result(self) -> TaskResult:
        if self._exc is not None:
            raise self._exc
        return self._result  # type: ignore[return-value]


class _FakeWorkerPlugin:
    """Stand-in base class for ``distributed.diagnostics.plugin.WorkerPlugin``."""

    name = "worker_plugin"


def _install_fake_distributed_package(monkeypatch) -> None:
    """Inject a fake ``distributed`` package tree into ``sys.modules``.

    ``_build_loguru_worker_plugin`` does a genuine
    ``from distributed.diagnostics.plugin import WorkerPlugin`` import
    (separate from the ``dask.distributed`` compatibility shim), which fails
    in environments where the real ``distributed`` package is not installed
    (only ``dask`` is). Providing a minimal fake here keeps these tests
    independent of whether the optional ``[batch]`` extras are installed.
    """
    fake_distributed = types.ModuleType("distributed")
    fake_diagnostics = types.ModuleType("distributed.diagnostics")
    fake_plugin_mod = types.ModuleType("distributed.diagnostics.plugin")
    fake_plugin_mod.WorkerPlugin = _FakeWorkerPlugin

    monkeypatch.setitem(sys.modules, "distributed", fake_distributed)
    monkeypatch.setitem(sys.modules, "distributed.diagnostics", fake_diagnostics)
    monkeypatch.setitem(sys.modules, "distributed.diagnostics.plugin", fake_plugin_mod)


def _install_fake_dask(monkeypatch, futures: list[_FakeFuture]):
    """Inject fake ``dask.distributed`` and ``distributed`` modules into ``sys.modules``.

    ``run_batch`` does ``from dask.distributed import Client, LocalCluster,
    as_completed, wait`` lazily, so we only need to provide those names. It
    also calls ``_build_loguru_worker_plugin``, which separately imports from
    the real ``distributed`` package — see ``_install_fake_distributed_package``.
    """
    _install_fake_distributed_package(monkeypatch)

    fake_mod = types.ModuleType("dask.distributed")

    fake_cluster = MagicMock(name="LocalCluster")
    fake_cluster.return_value = fake_cluster  # LocalCluster() returns the mock itself
    fake_cluster.adapt = MagicMock()

    fake_client = MagicMock(name="Client")
    fake_client_instance = MagicMock(name="client_instance")
    fake_client_instance.dashboard_link = "http://localhost:8787"
    fake_client_instance.scatter.return_value = [f.key for f in futures]
    fake_client_instance.map.return_value = futures
    fake_client_instance.register_plugin = MagicMock()
    # Client(cluster) is used as a context manager
    fake_client.return_value = fake_client_instance
    fake_client_instance.__enter__ = MagicMock(return_value=fake_client_instance)
    fake_client_instance.__exit__ = MagicMock(return_value=False)

    def fake_as_completed(futs):
        return iter(futs)

    fake_mod.Client = fake_client
    fake_mod.LocalCluster = fake_cluster
    fake_mod.as_completed = fake_as_completed
    fake_mod.wait = MagicMock(name="wait")

    monkeypatch.setitem(sys.modules, "dask.distributed", fake_mod)
    return fake_client, fake_cluster


@pytest.fixture()
def cfg(tmp_path):
    return BatchConfig(
        db_path=tmp_path / "tracker.db",
        workers_min=2,
        workers_max=4,
        retries=1,
        log_every=5,
        dashboard_port=0,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_batch_nothing_to_do(cfg, monkeypatch):
    """When all tasks are already DONE, run_batch returns without touching Dask."""
    tasks = _make_tasks(5)
    init_db(cfg.db_path)
    for t in tasks:
        mark_done(cfg.db_path, t["task_id"], "s3://atlantis/pre.tif")

    # The dask import happens at the top of run_batch (before the early
    # return), so we still need a valid fake module in sys.modules — but
    # the cluster/client should never be constructed.
    fake_client, fake_cluster = _install_fake_dask(monkeypatch, [])

    run_batch(tasks, process_fn=_succeed, cfg=cfg)

    fake_cluster.assert_not_called()
    fake_client.assert_not_called()
    s = stats(cfg.db_path)
    assert s["DONE"] == 5
    assert s.get("FAILED", 0) == 0


def test_run_batch_empty_task_list(cfg, monkeypatch):
    """An empty task list should be a no-op (nothing to do)."""
    # The dask import happens before the pending-tasks check, so a fake
    # module must still be importable even though it's never used.
    _install_fake_dask(monkeypatch, [])
    run_batch([], process_fn=_succeed, cfg=cfg)
    s = stats(cfg.db_path)
    assert s == {"total": 0}


def test_run_batch_all_succeed(cfg, monkeypatch):
    """All futures succeed → all tasks marked DONE in the tracker."""
    tasks = _make_tasks(10)
    futures = [_FakeFuture(t["task_id"], _succeed(t)) for t in tasks]
    _install_fake_dask(monkeypatch, futures)

    run_batch(tasks, process_fn=_succeed, cfg=cfg)

    s = stats(cfg.db_path)
    assert s["DONE"] == 10
    assert s.get("FAILED", 0) == 0


def test_run_batch_all_fail(cfg, monkeypatch):
    """All futures fail → all tasks marked FAILED in the tracker."""
    tasks = _make_tasks(5)
    futures = [_FakeFuture(t["task_id"], None, exc=RuntimeError("boom")) for t in tasks]
    _install_fake_dask(monkeypatch, futures)

    run_batch(tasks, process_fn=_fail, cfg=cfg)

    s = stats(cfg.db_path)
    assert s["FAILED"] == 5
    assert s.get("DONE", 0) == 0


def test_run_batch_mixed_success_and_failure(cfg, monkeypatch):
    """A mix of succeeding and failing futures is tracked correctly."""
    tasks = _make_tasks(6)
    futures = [
        _FakeFuture(tasks[0]["task_id"], _succeed(tasks[0])),
        _FakeFuture(tasks[1]["task_id"], None, exc=RuntimeError("e1")),
        _FakeFuture(tasks[2]["task_id"], _succeed(tasks[2])),
        _FakeFuture(tasks[3]["task_id"], None, exc=ValueError("e2")),
        _FakeFuture(tasks[4]["task_id"], _succeed(tasks[4])),
        _FakeFuture(tasks[5]["task_id"], _succeed(tasks[5])),
    ]
    _install_fake_dask(monkeypatch, futures)

    run_batch(tasks, process_fn=_succeed, cfg=cfg)

    s = stats(cfg.db_path)
    assert s["DONE"] == 4
    assert s["FAILED"] == 2
    assert s["total"] == 6


def test_run_batch_resume_skips_done(cfg, monkeypatch):
    """Pre-seeded DONE tasks are filtered out; only pending ones are submitted."""
    tasks = _make_tasks(10)
    init_db(cfg.db_path)
    for t in tasks[:4]:
        mark_done(cfg.db_path, t["task_id"], "s3://atlantis/pre.tif")

    pending = tasks[4:]
    futures = [_FakeFuture(t["task_id"], _succeed(t)) for t in pending]
    fake_client, _ = _install_fake_dask(monkeypatch, futures)

    run_batch(tasks, process_fn=_succeed, cfg=cfg)

    # Only the 6 pending tasks should have been scattered / mapped
    fake_client_instance = fake_client.return_value
    assert fake_client_instance.scatter.call_count == 1
    scattered_arg = fake_client_instance.scatter.call_args[0][0]
    assert len(scattered_arg) == 6

    s = stats(cfg.db_path)
    assert s["DONE"] == 10


def test_run_batch_progress_logging(cfg, monkeypatch):
    """Progress lines are emitted at the configured log_every interval."""
    # log_every=2 → progress logged at items 2, 4, 6, … and the last
    cfg.log_every = 2
    tasks = _make_tasks(4)
    futures = [_FakeFuture(t["task_id"], _succeed(t)) for t in tasks]
    _install_fake_dask(monkeypatch, futures)

    info_lines = []
    monkeypatch.setattr("atlantis.batch.orchestrator.logger.info", lambda *a: info_lines.append(a))

    run_batch(tasks, process_fn=_succeed, cfg=cfg)

    # Find progress lines (they contain the "[" prefix from the format string)
    progress_lines = [ln for ln in info_lines if isinstance(ln[0], str) and ln[0].startswith("[")]
    assert len(progress_lines) == 2  # at item 2 and item 4 (last)


def test_run_batch_failure_warning_logged(cfg, monkeypatch):
    """A warning is logged for each failed future."""
    tasks = _make_tasks(2)
    futures = [_FakeFuture(t["task_id"], None, exc=RuntimeError("boom")) for t in tasks]
    _install_fake_dask(monkeypatch, futures)

    warnings = []
    monkeypatch.setattr("atlantis.batch.orchestrator.logger.warning", lambda *a: warnings.append(a))

    run_batch(tasks, process_fn=_fail, cfg=cfg)

    assert len(warnings) == 2
    for w in warnings:
        assert "FAILED" in w[0]


def test_run_batch_cluster_configured_from_cfg(cfg, monkeypatch):
    """LocalCluster and cluster.adapt are called with values from BatchConfig."""
    tasks = _make_tasks(3)
    futures = [_FakeFuture(t["task_id"], _succeed(t)) for t in tasks]
    _, fake_cluster = _install_fake_dask(monkeypatch, futures)

    run_batch(tasks, process_fn=_succeed, cfg=cfg)

    fake_cluster.assert_called_once_with(
        n_workers=cfg.workers_min,
        threads_per_worker=1,
        memory_limit=cfg.memory_limit_per_worker,
        dashboard_address=f":{cfg.dashboard_port}",
    )
    fake_cluster.adapt.assert_called_once_with(minimum=cfg.workers_min, maximum=cfg.workers_max)


def test_run_batch_map_passes_retries_and_pure(cfg, monkeypatch):
    """client.map is called with retries and pure=False from the config."""
    tasks = _make_tasks(3)
    futures = [_FakeFuture(t["task_id"], _succeed(t)) for t in tasks]
    fake_client, _ = _install_fake_dask(monkeypatch, futures)

    run_batch(tasks, process_fn=_succeed, cfg=cfg)

    fake_client_instance = fake_client.return_value
    fake_client_instance.map.assert_called_once()
    _, kwargs = fake_client_instance.map.call_args
    assert kwargs["retries"] == cfg.retries
    assert kwargs["pure"] is False


def test_run_batch_registers_worker_plugin(cfg, monkeypatch):
    """The loguru worker plugin is registered on the client."""
    tasks = _make_tasks(2)
    futures = [_FakeFuture(t["task_id"], _succeed(t)) for t in tasks]
    fake_client, _ = _install_fake_dask(monkeypatch, futures)

    run_batch(tasks, process_fn=_succeed, cfg=cfg)

    fake_client_instance = fake_client.return_value
    fake_client_instance.register_plugin.assert_called_once()


def test_build_loguru_worker_plugin(monkeypatch):
    """The worker plugin factory returns a plugin with a setup method."""
    _install_fake_distributed_package(monkeypatch)
    from atlantis.batch.orchestrator import _build_loguru_worker_plugin

    plugin = _build_loguru_worker_plugin()
    assert plugin.name == "loguru_setup"
    assert hasattr(plugin, "setup")


def test_run_batch_final_stats_logged(cfg, monkeypatch):
    """A final summary line with DONE/FAILED/total is logged at the end."""
    tasks = _make_tasks(3)
    futures = [_FakeFuture(t["task_id"], _succeed(t)) for t in tasks]
    _install_fake_dask(monkeypatch, futures)

    info_lines = []
    monkeypatch.setattr("atlantis.batch.orchestrator.logger.info", lambda *a: info_lines.append(a))

    run_batch(tasks, process_fn=_succeed, cfg=cfg)

    summary = [ln for ln in info_lines if isinstance(ln[0], str) and "Batch complete" in ln[0]]
    assert len(summary) == 1
    # loguru passes the format string as arg[0] and the values as arg[1:]:
    # "Batch complete: DONE={} FAILED={} total={}", 3, 0, 3
    fmt, *args = summary[0]
    assert "DONE={}" in fmt
    assert "FAILED={}" in fmt
    assert "total={}" in fmt
    assert args == [3, 0, 3]
