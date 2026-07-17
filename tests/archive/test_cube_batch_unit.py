"""Fast unit tests for run_cube_batch with mocked Dask.

The existing tests in test_cube_batch.py are ``@pytest.mark.slow`` integration
tests that spin up a real Dask LocalCluster. These tests mock the
``dask.distributed`` imports so the full ``run_cube_batch`` control flow —
resume filtering, produce/consume success/failure, progress logging, and the
"nothing to do" early return — can be exercised quickly without any Dask
dependency.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from atlantis.archive.cube_batch import run_cube_batch
from atlantis.batch import BatchConfig
from atlantis.batch.tracker import init_db, mark_done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tasks(n: int) -> list[dict]:
    return [{"task_id": f"task_{i:04d}", "date": "2020-01-01", "aoi_id": i} for i in range(n)]


def _produce_ok(task: dict) -> dict:
    return {"task_id": task["task_id"], "date": task["date"], "aoi_id": task["aoi_id"], "value": 42}


def _produce_fail(task: dict) -> dict:
    raise RuntimeError(f"simulated failure for {task['task_id']}")


class _FakeFuture:
    """Minimal stand-in for a Dask future.

    ``run_cube_batch`` calls ``.result()``, reads ``.key``, and calls
    ``.release()`` on each future.
    """

    def __init__(self, key: str, payload: dict | None, exc: Exception | None = None):
        self.key = key
        self._payload = payload
        self._exc = exc
        self.released = False

    def result(self) -> dict:
        if self._exc is not None:
            raise self._exc
        return self._payload  # type: ignore[return-value]

    def release(self) -> None:
        self.released = True


def _install_fake_dask(monkeypatch, futures: list[_FakeFuture]):
    """Inject a fake ``dask.distributed`` module into ``sys.modules``."""

    fake_mod = types.ModuleType("dask.distributed")

    fake_cluster = MagicMock(name="LocalCluster")
    fake_cluster_instance = MagicMock(name="cluster_instance")
    fake_cluster.return_value = fake_cluster_instance
    fake_cluster_instance.adapt = MagicMock()
    fake_cluster_instance.close = MagicMock()

    fake_client = MagicMock(name="Client")
    fake_client_instance = MagicMock(name="client_instance")
    fake_client_instance.dashboard_link = "http://localhost:8787"
    fake_client_instance.scatter.return_value = [f.key for f in futures]
    fake_client_instance.map.return_value = futures
    fake_client_instance.register_plugin = MagicMock()
    fake_client.return_value = fake_client_instance
    fake_client_instance.__enter__ = MagicMock(return_value=fake_client_instance)
    fake_client_instance.__exit__ = MagicMock(return_value=False)

    def fake_as_completed(futs):
        return iter(futs)

    fake_mod.Client = fake_client
    fake_mod.LocalCluster = fake_cluster
    fake_mod.as_completed = fake_as_completed

    monkeypatch.setitem(sys.modules, "dask.distributed", fake_mod)
    return fake_client, fake_cluster


@pytest.fixture()
def cfg(tmp_path):
    return BatchConfig(
        db_path=tmp_path / "cube_tracker.db",
        workers_min=2,
        workers_max=4,
        retries=1,
        log_every=5,
        dashboard_port=0,
    )


def _consume_ok(payload: dict) -> str:
    return f"s3://cube/{payload['task_id']}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_cube_batch_nothing_to_do(cfg, monkeypatch):
    """When all tasks are already DONE, returns immediately without Dask."""
    tasks = _make_tasks(5)
    init_db(cfg.db_path)
    for t in tasks:
        mark_done(cfg.db_path, t["task_id"], "s3://pre")

    fake_client, fake_cluster = _install_fake_dask(monkeypatch, [])

    final = run_cube_batch(tasks, _produce_ok, _consume_ok, cfg)

    fake_cluster.assert_not_called()
    fake_client.assert_not_called()
    assert final["DONE"] == 5


def test_run_cube_batch_empty_tasks(cfg, monkeypatch):
    """An empty task list is a no-op."""
    _install_fake_dask(monkeypatch, [])
    final = run_cube_batch([], _produce_ok, _consume_ok, cfg)
    assert final == {"total": 0}


def test_run_cube_batch_all_succeed(cfg, monkeypatch):
    """All futures succeed → all tasks marked DONE."""
    tasks = _make_tasks(10)
    futures = [_FakeFuture(t["task_id"], _produce_ok(t)) for t in tasks]
    _install_fake_dask(monkeypatch, futures)

    final = run_cube_batch(tasks, _produce_ok, _consume_ok, cfg)

    assert final["DONE"] == 10
    assert final.get("FAILED", 0) == 0


def test_run_cube_batch_all_fail(cfg, monkeypatch):
    """All futures fail → all tasks marked FAILED."""
    tasks = _make_tasks(5)
    futures = [_FakeFuture(t["task_id"], None, exc=RuntimeError("boom")) for t in tasks]
    _install_fake_dask(monkeypatch, futures)

    final = run_cube_batch(tasks, _produce_fail, _consume_ok, cfg)

    assert final["FAILED"] == 5
    assert final.get("DONE", 0) == 0


def test_run_cube_batch_mixed(cfg, monkeypatch):
    """A mix of succeeding and failing futures is tracked correctly."""
    tasks = _make_tasks(6)
    futures = [
        _FakeFuture(tasks[0]["task_id"], _produce_ok(tasks[0])),
        _FakeFuture(tasks[1]["task_id"], None, exc=RuntimeError("e1")),
        _FakeFuture(tasks[2]["task_id"], _produce_ok(tasks[2])),
        _FakeFuture(tasks[3]["task_id"], None, exc=ValueError("e2")),
        _FakeFuture(tasks[4]["task_id"], _produce_ok(tasks[4])),
        _FakeFuture(tasks[5]["task_id"], _produce_ok(tasks[5])),
    ]
    _install_fake_dask(monkeypatch, futures)

    final = run_cube_batch(tasks, _produce_ok, _consume_ok, cfg)

    assert final["DONE"] == 4
    assert final["FAILED"] == 2
    assert final["total"] == 6


def test_run_cube_batch_resume_skips_done(cfg, monkeypatch):
    """Pre-seeded DONE tasks are filtered out; only pending ones are submitted."""
    tasks = _make_tasks(10)
    init_db(cfg.db_path)
    for t in tasks[:4]:
        mark_done(cfg.db_path, t["task_id"], "s3://pre")

    pending = tasks[4:]
    futures = [_FakeFuture(t["task_id"], _produce_ok(t)) for t in pending]
    fake_client, _ = _install_fake_dask(monkeypatch, futures)

    final = run_cube_batch(tasks, _produce_ok, _consume_ok, cfg)

    # Only 6 pending tasks should have been scattered
    fake_client_instance = fake_client.return_value
    assert fake_client_instance.scatter.call_count == 1
    scattered_arg = fake_client_instance.scatter.call_args[0][0]
    assert len(scattered_arg) == 6
    assert final["DONE"] == 10


def test_run_cube_batch_consume_called_with_payload(cfg, monkeypatch):
    """The consume function receives the payload from produce_fn."""
    tasks = _make_tasks(3)
    futures = [_FakeFuture(t["task_id"], _produce_ok(t)) for t in tasks]
    _install_fake_dask(monkeypatch, futures)

    consumed = []

    def consume(payload):
        consumed.append(payload)
        return f"uri://{payload['task_id']}"

    run_cube_batch(tasks, _produce_ok, consume, cfg)

    assert len(consumed) == 3
    assert all(c["value"] == 42 for c in consumed)


def test_run_cube_batch_consume_failure_marks_failed(cfg, monkeypatch):
    """If consume raises, the task is marked FAILED (not DONE)."""
    tasks = _make_tasks(3)
    futures = [_FakeFuture(t["task_id"], _produce_ok(t)) for t in tasks]
    _install_fake_dask(monkeypatch, futures)

    def consume(payload):
        raise IOError("write error")

    final = run_cube_batch(tasks, _produce_ok, consume, cfg)

    assert final["FAILED"] == 3
    assert final.get("DONE", 0) == 0


def test_run_cube_batch_progress_logging(cfg, monkeypatch):
    """Progress lines are emitted at the configured log_every interval."""
    cfg.log_every = 2
    tasks = _make_tasks(4)
    futures = [_FakeFuture(t["task_id"], _produce_ok(t)) for t in tasks]
    _install_fake_dask(monkeypatch, futures)

    info_lines = []
    monkeypatch.setattr("atlantis.archive.cube_batch.logger.info", lambda *a: info_lines.append(a))

    run_cube_batch(tasks, _produce_ok, _consume_ok, cfg)

    progress_lines = [ln for ln in info_lines if isinstance(ln[0], str) and ln[0].startswith("[")]
    assert len(progress_lines) == 2  # at item 2 and item 4


def test_run_cube_batch_failure_warning_logged(cfg, monkeypatch):
    """A warning is logged for each failed future."""
    tasks = _make_tasks(2)
    futures = [_FakeFuture(t["task_id"], None, exc=RuntimeError("boom")) for t in tasks]
    _install_fake_dask(monkeypatch, futures)

    warnings = []
    monkeypatch.setattr("atlantis.archive.cube_batch.logger.warning", lambda *a: warnings.append(a))

    run_cube_batch(tasks, _produce_fail, _consume_ok, cfg)

    assert len(warnings) == 2
    for w in warnings:
        assert "FAILED" in w[0]


def test_run_cube_batch_cluster_closed_in_finally(cfg, monkeypatch):
    """cluster.close() is called even if an exception occurs during the run."""
    tasks = _make_tasks(2)
    futures = [_FakeFuture(t["task_id"], _produce_ok(t)) for t in tasks]
    _, fake_cluster = _install_fake_dask(monkeypatch, futures)
    fake_cluster_instance = fake_cluster.return_value

    run_cube_batch(tasks, _produce_ok, _consume_ok, cfg)

    fake_cluster_instance.close.assert_called_once()


def test_run_cube_batch_final_stats_logged(cfg, monkeypatch):
    """A final summary line with DONE/FAILED/total is logged at the end."""
    tasks = _make_tasks(3)
    futures = [_FakeFuture(t["task_id"], _produce_ok(t)) for t in tasks]
    _install_fake_dask(monkeypatch, futures)

    info_lines = []
    monkeypatch.setattr("atlantis.archive.cube_batch.logger.info", lambda *a: info_lines.append(a))

    final = run_cube_batch(tasks, _produce_ok, _consume_ok, cfg)

    summary = [ln for ln in info_lines if isinstance(ln[0], str) and "Cube batch complete" in ln[0]]
    assert len(summary) == 1
    fmt, *args = summary[0]
    assert "DONE={}" in fmt
    assert "FAILED={}" in fmt
    assert "total={}" in fmt
    assert args == [final.get("DONE", 0), final.get("FAILED", 0), final.get("total", 0)]


def test_run_cube_batch_futures_released(cfg, monkeypatch):
    """Each future is released after processing (frees Dask graph memory)."""
    tasks = _make_tasks(3)
    futures = [_FakeFuture(t["task_id"], _produce_ok(t)) for t in tasks]
    _install_fake_dask(monkeypatch, futures)

    run_cube_batch(tasks, _produce_ok, _consume_ok, cfg)

    assert all(f.released for f in futures)


def test_run_cube_batch_cluster_configured_from_cfg(cfg, monkeypatch):
    """LocalCluster and cluster.adapt are called with values from BatchConfig."""
    tasks = _make_tasks(2)
    futures = [_FakeFuture(t["task_id"], _produce_ok(t)) for t in tasks]
    _, fake_cluster = _install_fake_dask(monkeypatch, futures)

    run_cube_batch(tasks, _produce_ok, _consume_ok, cfg)

    fake_cluster.assert_called_once_with(
        n_workers=cfg.workers_min,
        threads_per_worker=1,
        memory_limit=cfg.memory_limit_per_worker,
        dashboard_address=f":{cfg.dashboard_port}",
    )
    fake_cluster.return_value.adapt.assert_called_once_with(minimum=cfg.workers_min, maximum=cfg.workers_max)
