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
