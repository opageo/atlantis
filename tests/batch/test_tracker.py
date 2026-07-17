"""Unit tests for the SQLite tracker."""

import pytest

from atlantis.batch.tracker import get_pending, init_db, mark_done, mark_failed, stats


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


def test_init_creates_table(db):
    import sqlite3

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'").fetchall()
    assert len(rows) == 1


def test_get_pending_all_pending_when_db_empty(db):
    ids = {"a", "b", "c"}
    assert get_pending(db, ids) == ids


def test_mark_done_removes_from_pending(db):
    ids = {"a", "b", "c"}
    mark_done(db, "a", "s3://atlantis/a.tif")
    pending = get_pending(db, ids)
    assert pending == {"b", "c"}


def test_mark_done_idempotent(db):
    mark_done(db, "a", "s3://atlantis/a.tif")
    mark_done(db, "a", "s3://atlantis/a.tif")  # second call must not raise
    pending = get_pending(db, {"a", "b"})
    assert "a" not in pending


def test_mark_failed_stays_pending(db):
    ids = {"a", "b"}
    mark_failed(db, "a", "some error", attempts=3)
    # FAILED tasks are re-queued on restart
    pending = get_pending(db, ids)
    assert "a" in pending


def test_stats(db):
    mark_done(db, "a", "s3://atlantis/a.tif")
    mark_done(db, "b", "s3://atlantis/b.tif")
    mark_failed(db, "c", "error", attempts=3)
    s = stats(db)
    assert s["DONE"] == 2
    assert s["FAILED"] == 1
    assert s["total"] == 3


def test_get_pending_no_db(tmp_path):
    ids = {"x", "y"}
    result = get_pending(tmp_path / "nonexistent.db", ids)
    assert result == ids


def test_stats_no_db(tmp_path):
    """stats() returns {'total': 0} when the database file does not exist."""
    s = stats(tmp_path / "nonexistent.db")
    assert s == {"total": 0}


def test_stats_empty_db(db):
    """stats() on an initialised-but-empty database returns {'total': 0}."""
    s = stats(db)
    assert s == {"total": 0}


def test_mark_failed_updates_error_and_attempts(db):
    """mark_failed should persist the error message and attempt count."""
    mark_failed(db, "task_x", "RuntimeError: boom", attempts=4)
    import sqlite3

    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT status, error, attempts FROM tasks WHERE task_id = ?", ("task_x",)).fetchone()
    assert row == ("FAILED", "RuntimeError: boom", 4)


def test_mark_failed_idempotent_updates(db):
    """Re-marking a failed task updates the error/attempts rather than duplicating."""
    mark_failed(db, "task_a", "first error", attempts=1)
    mark_failed(db, "task_a", "second error", attempts=2)
    import sqlite3

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT status, error, attempts FROM tasks WHERE task_id = ?", ("task_a",)).fetchall()
    assert len(rows) == 1
    assert rows[0] == ("FAILED", "second error", 2)


def test_mark_done_updates_output_uri_on_conflict(db):
    """Re-marking a done task with a new output_uri should update the stored value."""
    mark_done(db, "task_a", "s3://atlantis/old.tif")
    mark_done(db, "task_a", "s3://atlantis/new.tif")
    import sqlite3

    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT status, output_uri, attempts FROM tasks WHERE task_id = ?", ("task_a",)).fetchone()
    assert row[0] == "DONE"
    assert row[1] == "s3://atlantis/new.tif"
    assert row[2] == 2  # attempts incremented


def test_get_pending_empty_set(db):
    """get_pending with an empty task-id set returns an empty set."""
    assert get_pending(db, set()) == set()


def test_get_pending_all_done(db):
    """get_pending returns an empty set when every task is DONE."""
    mark_done(db, "a", "s3://atlantis/a.tif")
    mark_done(db, "b", "s3://atlantis/b.tif")
    assert get_pending(db, {"a", "b", "c"}) == {"c"}


def test_init_db_creates_parent_directory(tmp_path):
    """init_db should create parent directories that don't yet exist."""
    nested = tmp_path / "deeply" / "nested" / "path" / "tracker.db"
    init_db(nested)
    assert nested.exists()


def test_stats_mixed_statuses(db):
    """stats correctly counts a mix of DONE and FAILED tasks."""
    mark_done(db, "a", "s3://atlantis/a.tif")
    mark_done(db, "b", "s3://atlantis/b.tif")
    mark_failed(db, "c", "error1", attempts=3)
    mark_failed(db, "d", "error2", attempts=2)
    s = stats(db)
    assert s["DONE"] == 2
    assert s["FAILED"] == 2
    assert s["total"] == 4
