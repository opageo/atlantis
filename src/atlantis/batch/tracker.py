"""SQLite-backed progress tracker for batch runs.

Owned exclusively by the main orchestrator process — Dask workers never
touch it.  Provides crash-safe resume: on restart, ``get_pending`` filters
out already-finished task IDs before new futures are submitted.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    output_uri  TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    finished_at TEXT
)
"""


def init_db(db_path: Path) -> None:
    """Create the tasks table if it does not yet exist."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(_CREATE_TABLE)
        conn.commit()


def mark_done(db_path: Path, task_id: str, output_uri: str) -> None:
    """Record a successfully completed task."""
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO tasks (task_id, status, output_uri, attempts, finished_at)
            VALUES (?, 'DONE', ?, 1, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                status      = 'DONE',
                output_uri  = excluded.output_uri,
                attempts    = tasks.attempts + 1,
                finished_at = excluded.finished_at
            """,
            (task_id, output_uri, now),
        )
        conn.commit()


def mark_failed(db_path: Path, task_id: str, error: str, attempts: int) -> None:
    """Record a permanently failed task (exhausted all Dask retries)."""
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO tasks (task_id, status, error, attempts, finished_at)
            VALUES (?, 'FAILED', ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                status      = 'FAILED',
                error       = excluded.error,
                attempts    = excluded.attempts,
                finished_at = excluded.finished_at
            """,
            (task_id, error, attempts, now),
        )
        conn.commit()


def get_pending(db_path: Path, all_task_ids: Iterable[str]) -> set[str]:
    """Return the subset of *all_task_ids* that are not yet marked DONE.

    Tasks with status FAILED are included so they can be retried on restart.
    """
    all_ids = set(all_task_ids)
    if not Path(db_path).exists():
        return all_ids
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT task_id FROM tasks WHERE status = 'DONE'").fetchall()
    done = {row[0] for row in rows}
    return all_ids - done


def stats(db_path: Path) -> dict[str, int]:
    """Return counts by status, plus a 'total' key."""
    if not Path(db_path).exists():
        return {"total": 0}
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall()
    result = {status: count for status, count in rows}
    result["total"] = sum(result.values())
    return result
