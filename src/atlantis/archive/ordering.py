"""Time-axis ordering helpers for the incremental archive update path.

The cube batch engine streams completed payloads through ``as_completed``, so a
plain writer session appends unseen dates to the ``time`` axis in completion
order. :class:`OrderedConsume` wraps a writer session so the axis only ever
grows in ascending date order, and :func:`unsorted_spans` reports violations
for post-write validation.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from atlantis.archive.cube_batch import _to_date


def unsorted_spans(times: np.ndarray) -> list[tuple[int, int]]:
    """Return inclusive index ranges where *times* is not strictly ascending.

    An empty list means the axis is fully sorted.
    """
    spans: list[tuple[int, int]] = []
    start: int | None = None
    prev: int | None = None
    for idx, t in enumerate(np.asarray(times)):
        if prev is not None and t <= prev:
            if start is None:
                start = idx - 1
        elif start is not None:
            spans.append((start, idx - 1))
            start = None
        prev = t
    if start is not None:
        spans.append((start, len(times) - 1))
    return spans


class OrderedConsume:
    """Buffer payloads and write them into a session in ascending date order.

    The engine's ``as_completed`` loop delivers payloads in completion order; a
    naive writer session would append unseen dates to the time axis in that
    order. This wrapper buffers payloads per date and only flushes a date once
    every earlier date in the run is fully resolved (all its expected tasks are
    ``DONE`` or ``FAILED`` in the SQLite tracker), so new time slots are always
    appended ascending. :meth:`drain` flushes any remainder after the batch run
    ends (by then every task has resolved, so ordering is guaranteed).

    Only dates inside the run's task list are ordering-relevant: dates already
    present on the axis are region-written into their existing slot and never
    move the axis.
    """

    def __init__(self, session: Any, db_path: Path, tasks: list[dict[str, Any]]) -> None:
        self._session = session
        self._db_path = Path(db_path)
        self._dates = sorted({_to_date(t["date"]) for t in tasks})
        self._expected = Counter(_to_date(t["date"]).strftime("%Y%m%d") for t in tasks)
        self._by_id = {t["task_id"]: _to_date(t["date"]).strftime("%Y%m%d") for t in tasks}
        self._buffer: dict[date, list[tuple[Any, date]]] = {}

    def write(self, dataset: Any, time: date) -> None:
        """Queue one payload; flush any dates whose turn has come."""
        self._buffer.setdefault(time, []).append((dataset, time))
        self._flush()

    def drain(self) -> None:
        """Flush everything still buffered, ascending (run is over)."""
        for day in sorted(self._buffer):
            for dataset, t in self._buffer[day]:
                self._session.write(dataset, time=t)
        self._buffer.clear()

    def _flush(self) -> None:
        while True:
            flushable = sorted(day for day in self._buffer if self._earlier_resolved(day))
            if not flushable:
                return
            day = flushable[0]
            for dataset, t in self._buffer.pop(day):
                self._session.write(dataset, time=t)

    def _earlier_resolved(self, day: date) -> bool:
        """True when every earlier run date has all its expected tasks resolved.

        Resolved dates are derived from the task map (``self._by_id``) instead
        of parsed out of task ids, so any task-id scheme works — MODIS
        (``modis-20260101-...``) and GFM (``gfm-EMSR712-10-...-20241029``)
        alike. Task ids in the tracker that are not part of this run are
        ignored.
        """
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("SELECT task_id FROM tasks WHERE status IN ('DONE', 'FAILED')").fetchall()
        resolved = Counter()
        for (task_id,) in rows:
            key = self._by_id.get(task_id)
            if key is not None:
                resolved[key] += 1
        resolved = {key for key, count in resolved.items() if count >= self._expected.get(key, 0)}
        target = day.strftime("%Y%m%d")
        for earlier in self._dates:
            key = earlier.strftime("%Y%m%d")
            if key >= target:
                break
            if key not in resolved:
                return False
        return True
