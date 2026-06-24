"""Integration test for the Dask orchestrator with a synthetic process function."""

import pytest

from atlantis.batch import BatchConfig, TaskResult, run_batch


def _succeed(task: dict) -> TaskResult:
    return TaskResult(task_id=task["task_id"], output_uri=f"s3://atlantis/{task['task_id']}.tif")


def _fail_once(task: dict) -> TaskResult:
    """Fail on first attempt (simulates transient error); succeed on retry."""
    # We can't track state in a picklable function without external storage,
    # so this function just raises immediately to test the failure path.
    raise RuntimeError("simulated transient error")


@pytest.fixture()
def cfg(tmp_path):
    return BatchConfig(
        db_path=tmp_path / "tracker.db",
        workers_min=2,
        workers_max=2,
        retries=1,
        log_every=5,
        dashboard_port=0,  # disable dashboard in tests
    )


def _make_tasks(n: int) -> list[dict]:
    return [{"task_id": f"task_{i:04d}", "source_uri": f"s3://test/{i}", "dest_key": f"test/{i}.tif"} for i in range(n)]


@pytest.mark.slow
def test_run_batch_all_succeed(cfg):
    tasks = _make_tasks(20)
    run_batch(tasks, process_fn=_succeed, cfg=cfg)

    from atlantis.batch.tracker import stats

    s = stats(cfg.db_path)
    assert s.get("DONE", 0) == 20
    assert s.get("FAILED", 0) == 0


@pytest.mark.slow
def test_run_batch_resume_skips_done(cfg):
    tasks = _make_tasks(10)
    # Pre-seed 5 tasks as done.
    from atlantis.batch.tracker import init_db, mark_done

    init_db(cfg.db_path)
    for t in tasks[:5]:
        mark_done(cfg.db_path, t["task_id"], "s3://atlantis/pre.tif")

    run_batch(tasks, process_fn=_succeed, cfg=cfg)

    from atlantis.batch.tracker import stats

    s = stats(cfg.db_path)
    assert s.get("DONE", 0) == 10


@pytest.mark.slow
def test_run_batch_all_fail(cfg):
    tasks = _make_tasks(5)
    run_batch(tasks, process_fn=_fail_once, cfg=cfg)

    from atlantis.batch.tracker import stats

    s = stats(cfg.db_path)
    assert s.get("FAILED", 0) == 5
    assert s.get("DONE", 0) == 0
