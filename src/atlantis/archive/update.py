"""Incremental MODIS yearly archive updates (orchestration).

Wraps the existing building blocks — ``batch modis catalog`` (LAADS inventory),
the resume-safe cube batch engine, the SQLite tracker, and ``ArchiveWriter`` —
into a yearly update flow:

1. refresh + merge the year's yearly catalogue (candidate-then-promote);
2. reconcile expected task IDs against the tracker and the archive (requeue
   ``DONE``-but-missing tasks, report orphans);
3. process only unresolved work through the cube engine with an ordered writer
   (ascending time axis);
4. validate, advance the contiguous watermark, and write an immutable run
   manifest; back up tracker/manifest/catalogue in a ``finally`` path.

Invariants: one writer per MODIS year (per-year lock), the tracker is the
task-level source of truth, a ``DONE`` task is trusted only when its date
exists on the archive axis, and an older missing date is never appended at the
physical end of the time axis (append-only policy — earlier holes require
``atlantis archive modis _reindex-time`` first).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
import requests
from loguru import logger

from atlantis.archive import datacube, grid
from atlantis.archive._store import is_remote, store_for
from atlantis.archive.cube_batch import _payload_to_dataset, _to_date, run_cube_batch
from atlantis.archive.ordering import OrderedConsume, unsorted_spans
from atlantis.archive.writer import ArchiveWriter
from atlantis.batch import BatchConfig
from atlantis.batch.catalog import DEFAULT_S3_ENDPOINT, load_catalogue, write_catalogue
from atlantis.batch.tracker import init_db, requeue, stats
from atlantis.fetchers.modis.backend import MissingEarthdataTokenError
from atlantis.fetchers.modis.batch_processor import harmonise_modis_granule_payload, probe_download
from atlantis.fetchers.modis.catalog import build_catalog
from atlantis.fetchers.modis.inventory import to_tasks
from atlantis.fetchers.modis.processor import tile_bounds_from_hv
from atlantis.utils.io import DownloadContentError

MODIS_VAR_NAMES = ("water_fraction", "exclusion_mask", "reference_water", "recurring_flood")

#: Lock is stale when its owning PID is gone or it is older than this.
LOCK_MAX_AGE = timedelta(hours=24)

#: Catch-up guardrail: an auto-resolved window processes at most this many
#: calendar days, anchored at the newest end. Larger backlogs warn and need
#: explicit ``--start/--end`` backfill windows.
MAX_CATCHUP_DAYS = 31

_REQUIRED_CATALOGUE_COLUMNS = ("date", "h", "v", "task_id", "source_uri")


class UpdateError(RuntimeError):
    """Fatal condition in the update flow — maps to a non-zero CLI exit."""


@dataclass
class UpdateOptions:
    """Resolved options for an update run (mirrors the CLI surface)."""

    year: int | None = None
    start: date | None = None
    end: date | None = None
    lookback_days: int = 14
    availability_lag_days: int = 7
    archive_base: str = "s3://atlantis/zarr"
    state_root: Path = Path("/mnt/atlantis-state/modis")
    catalogue_base: str = "s3://atlantis/assets/modis"
    backup_base: str = "s3://atlantis/archive-state/modis"
    workers_min: int = 2
    workers_max: int = 6
    memory_limit: str = "2.5GB"
    dashboard_port: int = 8788
    retries: int = 3
    log_every: int = 50
    dry_run: bool = False
    retry_failed: bool = True
    storage_options: dict[str, Any] | None = None
    catalogue_builder: Callable | None = None  # injectable for tests
    today: date | None = None  # injectable clock for tests


@dataclass(frozen=True)
class YearWindow:
    """One selected archive year's update window."""

    year: int
    start: date
    end: date
    kind: str  # "reconciliation" | "catch-up" | "weekly"


@dataclass
class ReconcileReport:
    """Task-level classification result for a window."""

    expected: int = 0
    done: int = 0
    failed: int = 0
    pending: int = 0
    requeued: int = 0
    orphan_dates: list[date] = field(default_factory=list)


# ── Paths and small helpers ──────────────────────────────────────────────────


def year_state_dir(opts: UpdateOptions, year: int) -> Path:
    return opts.state_root / str(year)


def tracker_path(opts: UpdateOptions, year: int) -> Path:
    return year_state_dir(opts, year) / "cube_tracker.db"


def lock_path(opts: UpdateOptions, year: int) -> Path:
    return year_state_dir(opts, year) / "update.lock"


def manifests_dir(opts: UpdateOptions, year: int) -> Path:
    return year_state_dir(opts, year) / "manifests"


def logs_dir(opts: UpdateOptions, year: int) -> Path:
    return year_state_dir(opts, year) / "logs"


def local_catalogue_path(opts: UpdateOptions, year: int) -> Path:
    return year_state_dir(opts, year) / "catalogues" / f"modis-{year}.parquet"


def archive_root(opts: UpdateOptions, year: int) -> str:
    return f"{opts.archive_base.rstrip('/')}/{year}"


def catalogue_uri(opts: UpdateOptions, year: int) -> str:
    return f"{opts.catalogue_base.rstrip('/')}/modis_archive_catalog_{year}.parquet"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_revision() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=False)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 - best-effort metadata
        return None


def _object_exists(uri: str | Path, opts: UpdateOptions) -> bool:
    """True when a catalogue object exists (local path or s3:// URI)."""
    uri_str = str(uri)
    if not is_remote(uri_str):
        return Path(uri_str).exists()
    import s3fs

    storage_options = opts.storage_options or {"endpoint_url": DEFAULT_S3_ENDPOINT}
    return s3fs.S3FileSystem(**storage_options).exists(uri_str)


# ── Lock ─────────────────────────────────────────────────────────────────────


class YearLock:
    """Exclusive per-year update lock backed by a pid + timestamp file.

    A lock left by a dead PID (or older than :data:`LOCK_MAX_AGE`) is stale and
    is reclaimed automatically; a live lock fails the run.
    """

    def __init__(self, opts: UpdateOptions, year: int) -> None:
        self._path = lock_path(opts, year)
        self._year = year
        self._acquired = False

    def __enter__(self) -> YearLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            state = self.describe(self._path)
            if not state["stale"]:
                raise UpdateError(
                    f"another update is running for year {self._year} "
                    f"(lock {self._path} — inspect with `status` or remove the file)"
                ) from None
            logger.warning("Removing stale lock for year {}: {}", self._year, state)
            self._path.unlink(missing_ok=True)
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "started_at": _now_iso()}, f)
        self._acquired = True
        return self

    def __exit__(self, *exc: object) -> bool:
        if self._acquired:
            self._path.unlink(missing_ok=True)
            self._acquired = False
        return False

    @staticmethod
    def describe(path: Path) -> dict[str, Any]:
        """Return ``{"pid", "started_at", "stale"}`` for the lock file at *path*.

        An absent, unreadable, or malformed lock is treated as stale.
        """
        try:
            data = json.loads(Path(path).read_text())
        except Exception:  # noqa: BLE001 - unreadable lock is stale
            return {"pid": None, "started_at": None, "stale": True}
        pid = data.get("pid")
        started = data.get("started_at")
        try:
            os.kill(int(pid), 0)
            alive = True
        except (ProcessLookupError, TypeError, ValueError):
            alive = False
        age: timedelta | None = None
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(str(started))
        except (ValueError, TypeError):
            pass
        return {
            "pid": pid,
            "started_at": started,
            "stale": not alive or (age is not None and age > LOCK_MAX_AGE),
        }


# ── Window resolution ────────────────────────────────────────────────────────


def last_complete_for_year(opts: UpdateOptions, year: int) -> date | None:
    """Highest contiguous fully-``DONE`` date from the year's start, or None."""
    db = tracker_path(opts, year)
    if not db.exists():
        return None
    df = _load_year_catalogue(opts, year)
    if df is None or df.empty:
        return None
    done = {tid for tid, status in read_tracker(db).items() if status == "DONE"}
    return contiguous_complete_end(df, done, date(year, 1, 1))


def contiguous_complete_end(df: pd.DataFrame, done_ids: set[str], from_date: date) -> date | None:
    """Highest date such that every expected task from *from_date* up to it is DONE.

    Breaks at the first date with any expected task not in *done_ids* — a later
    completed date never skips an earlier gap.
    """
    rows = df[pd.to_datetime(df["date"]).dt.date >= from_date]
    last: date | None = None
    for day, ids in _expected_by_date(rows).items():
        if ids <= done_ids:
            last = day
        else:
            break
    return last


def resolve_windows(opts: UpdateOptions) -> list[YearWindow]:
    """Resolve the requested year/window into per-year windows, chronological.

    Auto-resolved windows (no explicit ``--start/--end``) are capped by the
    catch-up guardrail: a backlog longer than :data:`MAX_CATCHUP_DAYS` runs
    only the newest days and warns the operator. Explicit windows are
    deliberate backfills and are never capped. A fresh current year reaches
    back into the previous year, so a December backlog and the new year's
    first days are both covered automatically (the new year is prepared —
    catalogue + prefill — by :func:`_run_year`).
    """
    today = opts.today or date.today()
    lag_end = today - timedelta(days=opts.availability_lag_days)

    if opts.start is not None or opts.end is not None:
        start = opts.start or date(lag_end.year, 1, 1)
        end = opts.end or lag_end
        if start > end:
            return []
        kind = "reconciliation"
        windows = _clip_years(start, end, kind)
        return [w for w in windows if opts.year is None or w.year == opts.year]

    if opts.year is not None:
        last = last_complete_for_year(opts, opts.year)
        start = _window_start(opts, opts.year, last)
        end = min(lag_end, date(opts.year, 12, 31))
        kind = "catch-up" if last is None else "weekly"
        raw = start
        start, capped = _cap_window(start, end)
        if capped:
            _warn_catchup(raw, start, end)
        if start > end:
            return []
        return [YearWindow(opts.year, start, end, kind)]

    current = lag_end.year
    last = last_complete_for_year(opts, current)
    start = _window_start(opts, current, last)
    if last is None:
        prev = _window_start(opts, current - 1, last_complete_for_year(opts, current - 1))
        start = min(start, prev)
    end = lag_end
    raw = start
    start, capped = _cap_window(start, end)
    if capped:
        _warn_catchup(raw, start, end)
    if start > end:
        return []
    return _clip_years(start, end, "weekly")


def _window_start(opts: UpdateOptions, year: int, last: date | None) -> date:
    if last is None:
        return date(year, 1, 1)
    return max(date(year, 1, 1), last + timedelta(days=1 - opts.lookback_days))


def _cap_window(start: date, end: date) -> tuple[date, bool]:
    """Cap *start* so the window spans at most MAX_CATCHUP_DAYS, anchored at *end*."""
    capped = max(start, end - timedelta(days=MAX_CATCHUP_DAYS - 1))
    return capped, capped > start


def _warn_catchup(raw_start: date, start: date, end: date) -> None:
    """Operator-visible warning when the guardrail caps an auto-resolved window."""
    print(
        f"⚠  {(end - raw_start).days + 1} day(s) of backlog exceed the {MAX_CATCHUP_DAYS}-day "
        f"guardrail — processing only the newest {MAX_CATCHUP_DAYS} day(s) ({start} → {end}); "
        "backfill older dates with explicit --start/--end windows"
    )


def _clip_years(start: date, end: date, kind: str) -> list[YearWindow]:
    return [
        YearWindow(year, max(start, date(year, 1, 1)), min(end, date(year, 12, 31)), kind)
        for year in range(start.year, end.year + 1)
        if max(start, date(year, 1, 1)) <= min(end, date(year, 12, 31))
    ]


# ── Catalogue ────────────────────────────────────────────────────────────────


def _load_year_catalogue(opts: UpdateOptions, year: int) -> pd.DataFrame | None:
    """Local state catalogue if present, else the canonical per-year object."""
    local = local_catalogue_path(opts, year)
    if local.exists():
        return pd.read_parquet(local)
    uri = catalogue_uri(opts, year)
    if _object_exists(uri, opts):
        return load_catalogue(uri)
    return None


def refresh_catalogue(
    opts: UpdateOptions,
    year: int,
    start: date,
    end: date,
    run_id: str,
) -> tuple[pd.DataFrame, str]:
    """Build the fresh LAADS range, merge into the yearly catalogue, promote.

    The canonical per-year object is replaced only after the merged candidate
    validates (candidate-then-promote); a failed build leaves it intact.

    Returns:
        ``(full-year catalogue df, sha256 of the promoted bytes)``.
    """
    builder = opts.catalogue_builder or build_catalog
    cdir = year_state_dir(opts, year) / "catalogues"
    cdir.mkdir(parents=True, exist_ok=True)
    fresh = cdir / f"fresh-{run_id}.parquet"

    logger.info("Refreshing catalogue {} → {} for {}", start, end, year)
    builder(start=start.isoformat(), end=end.isoformat(), output=str(fresh), on_progress=None)
    new_rows = pd.read_parquet(fresh)

    existing = None
    uri = catalogue_uri(opts, year)
    if _object_exists(uri, opts):
        existing = load_catalogue(uri)

    combined = new_rows if existing is None else pd.concat([existing, new_rows], ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"]).dt.strftime("%Y-%m-%d")
    # Same tile republished by LAADS: the fresh row (later in the concat) wins.
    combined = combined.drop_duplicates(subset=["date", "h", "v"], keep="last")
    combined = combined.sort_values(["date", "h", "v"], ignore_index=True)
    year_start, year_end = f"{year}-01-01", f"{year}-12-31"
    combined = combined[(combined["date"] >= year_start) & (combined["date"] <= year_end)]
    _validate_catalogue(combined, year)

    local = local_catalogue_path(opts, year)
    combined.to_parquet(local, index=False)
    checksum = _sha256(local)
    write_catalogue(combined, uri, storage_options=opts.storage_options)
    logger.info("Published {} ({} rows, sha256 {})", uri, len(combined), checksum)
    return combined, checksum


def _validate_catalogue(df: pd.DataFrame, year: int) -> None:
    missing = [c for c in _REQUIRED_CATALOGUE_COLUMNS if c not in df.columns]
    if missing:
        raise UpdateError(f"catalogue for {year} is missing columns {missing}")
    if df.empty:
        raise UpdateError(f"catalogue for {year} is empty after merge")
    lo, hi = df["date"].min(), df["date"].max()
    if lo < f"{year}-01-01" or hi > f"{year}-12-31":
        raise UpdateError(f"catalogue rows outside year {year}: {lo} … {hi}")


# ── Reconciliation ───────────────────────────────────────────────────────────


def read_tracker(db_path: Path) -> dict[str, str]:
    """Return ``{task_id: status}`` for every tracked task."""
    if not Path(db_path).exists():
        return {}
    with sqlite3.connect(db_path) as conn:
        return dict(conn.execute("SELECT task_id, status FROM tasks").fetchall())


def reconcile_window(
    tasks: list[dict[str, Any]],
    tracker_rows: dict[str, str],
    archive_dates: set[date],
    year_dates: set[date],
    db_path: Path,
    warn: Callable[[str], None] = logger.warning,
    *,
    prefilled: bool = False,
) -> ReconcileReport:
    """Classify expected tasks vs tracker + archive; requeue DONE-but-missing.

    A date whose expected tasks are all ``DONE`` in the tracker but is absent
    from the archive axis is inconsistent: its tasks are requeued (row deleted)
    after an operator-visible warning, and the watermark must not advance past
    it. Archive dates with no coverage in the *full-year* catalogue (not just
    the window) are reported as orphans (never deleted).

    On a *prefilled* year (``atlantis_time_prefill`` marker) the axis holds
    every day of the year by construction, so "on the axis but not in the
    catalogue" is not evidence of stray data — pass ``prefilled=True`` to
    skip the orphan computation (it would report the whole unwritten tail of
    the year every run).
    """
    expected_ids = {t["task_id"] for t in tasks}
    done = {tid for tid, status in tracker_rows.items() if status == "DONE"}
    failed = {tid for tid, status in tracker_rows.items() if status == "FAILED"}
    report = ReconcileReport(
        expected=len(expected_ids),
        done=len(expected_ids & done),
        failed=len(expected_ids & failed),
    )

    by_date: dict[date, set[str]] = {}
    for t in tasks:
        by_date.setdefault(_to_date(t["date"]), set()).add(t["task_id"])

    for day, ids in sorted(by_date.items()):
        if day in archive_dates or not ids:
            continue
        if ids <= done:
            warn(f"date {day}: all {len(ids)} tasks DONE in tracker but missing from archive — requeueing")
            for tid in sorted(ids):
                requeue(db_path, tid)
            report.requeued += len(ids)

    if prefilled:
        report.orphan_dates = []
    else:
        report.orphan_dates = sorted(day for day in archive_dates if day not in year_dates)
    report.pending = len(expected_ids - done - failed) - report.requeued
    return report


# ── Archive reads and validation ─────────────────────────────────────────────


def read_archive_dates(
    opts: UpdateOptions,
    year: int,
    group: Any | None = None,
) -> tuple[set[date], list[date]]:
    """Return ``(dates on the year's modis time axis, sorted axis list)``.

    A year whose archive store does not exist yet (or has no ``modis`` group)
    yields an empty axis. Pass an already-opened *group* (e.g. from
    :func:`_modis_group`, to also read the prefill marker) to avoid a second
    store open.
    """
    if group is None:
        group = _modis_group(opts, year)
    if group is None:
        return set(), []
    axis = sorted(datacube.decode_axis_dates(group))
    return set(axis), axis


def _modis_group(opts: UpdateOptions, year: int) -> Any | None:
    """Open the year's ``modis`` group read-only, or None when absent."""
    import zarr

    store = store_for(archive_root(opts, year), "datacube.zarr", opts.storage_options)
    try:
        return datacube.open_root(store, mode="r")["modis"]
    except (KeyError, FileNotFoundError, zarr.errors.GroupNotFoundError):
        return None


def group_is_prefilled(group: Any) -> bool:
    """True when *group* carries the full-year prefill marker.

    The marker (``atlantis_time_prefill``) is written only by
    :func:`~atlantis.archive.datacube.prefill_year_axis` on an actual resize,
    so legacy prefilled groups without it count as non-prefilled.
    """
    return "atlantis_time_prefill" in group.attrs


def check_holes(opts: UpdateOptions, window: YearWindow, expected_dates: set[date], archive_dates: set[date]) -> None:
    """Refuse to append a missing date below the existing axis tail (policy 2).

    Earlier holes are a repair condition: the operator runs the offline
    ``_reindex-time`` migration (which inserts the missing slots) and re-runs.
    """
    if not archive_dates:
        return
    axis_max = max(archive_dates)
    holes = sorted(d for d in expected_dates - archive_dates if d < axis_max)
    if holes:
        cmd = f"`atlantis archive modis _reindex-time --year {opts.year or window.year}`"
        raise UpdateError(
            f"append-only policy: earlier hole(s) {holes[0]} … {holes[-1]} below axis tail {axis_max} "
            f"require the offline migration first: {cmd}"
        )


def sample_tasks(tasks: list[dict[str, Any]], done_ids: set[str], limit: int = 3) -> list[dict[str, Any]]:
    """One DONE task per date, up to *limit* dates, for non-NODATA sampling."""
    by_date: dict[date, dict[str, Any]] = {}
    for t in tasks:
        if t["task_id"] in done_ids:
            by_date.setdefault(_to_date(t["date"]), t)
    return [by_date[d] for d in sorted(by_date)[:limit]]


def check_samples(opts: UpdateOptions, year: int, samples: list[dict[str, Any]]) -> None:
    """Warn when a sampled DONE tile window is entirely NODATA (diagnostic)."""
    if not samples:
        return
    store = store_for(archive_root(opts, year), "datacube.zarr", opts.storage_options)
    group = datacube.open_root(store, mode="r")["modis"]
    units = group["time"].attrs.get("units", "days since 2020-01-01")
    epoch = str(units).rsplit("since ", 1)[-1].strip()
    times = np.asarray(group["time"][:], dtype="int64")
    arr = group["water_fraction"]
    for t in samples:
        time_idx = int(np.where(times == datacube.date_to_int(_to_date(t["date"]), epoch))[0][0])
        west, south, east, north = tile_bounds_from_hv(int(t["h"]), int(t["v"]))
        window = grid.bounds_to_window(west, south, east, north)
        block = arr[time_idx, window.row_start : window.row_stop, window.col_start : window.col_stop]
        if not np.any(block != 255):
            logger.warning("sample {} is all-NODATA in the archive", t["task_id"])


def validate_year(
    opts: UpdateOptions,
    window: YearWindow,
    tasks: list[dict[str, Any]],
    db_path: Path,
) -> None:
    """Assert completion, axis presence, and ascending time order."""
    expected = {t["task_id"] for t in tasks}
    tracker_rows = read_tracker(db_path)
    done = {tid for tid, status in tracker_rows.items() if status == "DONE"}
    failed_in_window = {tid for tid, status in tracker_rows.items() if status == "FAILED"} & expected
    missing = expected - done
    if missing or failed_in_window:
        raise UpdateError(f"validation failed: {len(missing)} task(s) not DONE, {len(failed_in_window)} FAILED")

    archive_dates, axis = read_archive_dates(opts, window.year)
    absent = sorted({_to_date(t["date"]) for t in tasks} - archive_dates)
    if absent:
        raise UpdateError(f"validation failed: expected dates missing from archive axis: {absent}")
    spans = unsorted_spans(axis)
    if spans:
        raise UpdateError(f"validation failed: time axis not ascending at {spans}")


# ── Batch ────────────────────────────────────────────────────────────────────


def _probe_pending_download(
    tasks: list[dict[str, Any]],
    db_path: Path,
    attempts: int = 3,
) -> None:
    """Preflight one real tile download before the batch launches.

    LAADS rejects a misconfigured token / unaccepted archive license only at
    download time, not at listing time, so the failure would otherwise surface
    as every tile FAILING inside Dask. Stream just the first chunk of the
    first non-DONE tile and fail fast with an actionable message instead.

    Transient failures (connection/timeout, 404/5xx, a flaky HTML/empty first
    chunk) are retried with short backoff; only a persistent auth/EULA signal
    (HTML/empty body, HTTP 401/403, missing token) aborts the run.

    Raises:
        UpdateError: When the probe persistently returns an auth/EULA signal.
    """
    tracker = read_tracker(db_path)
    todo = [t for t in tasks if tracker.get(t["task_id"]) != "DONE"]
    if not todo:
        return
    uri = todo[0]["source_uri"]
    last_issue: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            probe_download(uri)
            return
        except MissingEarthdataTokenError as exc:
            raise UpdateError(str(exc)) from exc
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (401, 403):
                raise UpdateError(
                    f"LAADS rejected the download token (HTTP {status}) for {uri} — "
                    "check that EARTHDATA_TOKEN is a valid LAADS application token "
                    "and the archive license is accepted"
                ) from exc
            last_issue = exc  # 404 / 5xx: per-granule or server-side; tiles retry individually
        except (DownloadContentError, requests.ConnectionError, requests.Timeout) as exc:
            last_issue = exc
        if attempt < attempts:
            time.sleep(attempt)
            logger.warning(
                "download preflight for {} failed on attempt {}/{} ({}) — retrying",
                uri,
                attempt,
                attempts,
                last_issue,
            )
    if isinstance(last_issue, DownloadContentError):
        raise UpdateError(str(last_issue)) from last_issue
    logger.warning(
        "download preflight for {} failed after {}/{} attempt(s) ({}) — continuing",
        uri,
        attempts,
        attempts,
        last_issue,
    )


def run_window_batch(opts: UpdateOptions, year: int, tasks: list[dict[str, Any]], db_path: Path) -> dict[str, int]:
    """Run the cube batch for *tasks* with an ascending-order writer session."""
    archive = archive_root(opts, year)
    writer = ArchiveWriter(archive, None, storage_options=opts.storage_options)
    cfg = BatchConfig(
        db_path=db_path,
        workers_min=opts.workers_min,
        workers_max=opts.workers_max,
        memory_limit_per_worker=opts.memory_limit,
        dashboard_port=opts.dashboard_port,
        retries=opts.retries,
        log_every=opts.log_every,
    )
    with writer.session("modis", list(MODIS_VAR_NAMES), prefill_year=year) as session:
        ordered = OrderedConsume(session, db_path, tasks)

        def consume(payload: dict[str, Any]) -> str:
            ordered.write(_payload_to_dataset(payload), time=_to_date(payload["date"]))
            return f"{archive}#modis/{payload['date']}/h{int(payload['h']):02d}v{int(payload['v']):02d}"

        final = run_cube_batch(tasks, harmonise_modis_granule_payload, consume, cfg)
        ordered.drain()
    return final


# ── Manifest and backup ──────────────────────────────────────────────────────


def write_manifest(
    opts: UpdateOptions,
    window: YearWindow,
    run_id: str,
    *,
    df: pd.DataFrame | None,
    checksum: str | None,
    final: dict[str, int] | None,
    watermark: date | None,
    axis_sorted: bool,
    orphans: list[date],
    status: str,
    started_at: str,
    failed: str | None = None,
) -> Path:
    manifest = {
        "run_id": run_id,
        "source": "modis",
        "year": window.year,
        "window": {"start": window.start.isoformat(), "end": window.end.isoformat(), "kind": window.kind},
        "archive_root": archive_root(opts, window.year),
        "tracker": str(tracker_path(opts, window.year)),
        "catalogue_uri": catalogue_uri(opts, window.year),
        "catalogue_checksum": checksum,
        "catalogue_rows": None if df is None else int(len(df)),
        "dask": {
            "workers_min": opts.workers_min,
            "workers_max": opts.workers_max,
            "memory_limit": opts.memory_limit,
            "dashboard_port": opts.dashboard_port,
            "retries": opts.retries,
        },
        "task_totals": final or {},
        "watermark": watermark.isoformat() if watermark else None,
        "time_axis_sorted": axis_sorted,
        "orphan_dates": [d.isoformat() for d in orphans],
        "status": status,
        "failed": failed,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "pipeline_revision": _git_revision(),
    }
    path = manifests_dir(opts, window.year) / f"{run_id}.json"
    seq = 2
    while path.exists():
        path = manifests_dir(opts, window.year) / f"{run_id}-{seq}.json"  # immutable: never overwrite
        seq += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2))
    logger.info("Wrote manifest → {}", path)
    return path


def backup_state(opts: UpdateOptions, year: int, *, manifest: Path | None) -> bool:
    """Mirror tracker, manifest, and catalogue to the backup root.

    Runs in a ``finally`` path (also on failure). Returns True on success so
    callers can fail a successful run whose backup failed.
    """
    sources = [tracker_path(opts, year), manifest, local_catalogue_path(opts, year)]
    sources = [p for p in sources if p is not None and Path(p).exists()]
    try:
        for src in sources:
            _copy_to(opts, f"{opts.backup_base.rstrip('/')}/{year}", src)
        logger.info("Backed up {} file(s) to {}", len(sources), opts.backup_base)
        return True
    except Exception as exc:  # noqa: BLE001 - backup must not mask the run result
        logger.error("State backup failed: {}", exc)
        return False


def _copy_to(opts: UpdateOptions, dest_dir: str, src: Path) -> None:
    dest = f"{dest_dir}/{src.name}" if is_remote(dest_dir) else Path(dest_dir) / src.name
    if is_remote(dest_dir):
        import s3fs

        storage_options = opts.storage_options or {"endpoint_url": DEFAULT_S3_ENDPOINT}
        s3fs.S3FileSystem(**storage_options).put(str(src), dest)
    else:
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


# ── Worker ───────────────────────────────────────────────────────────────────


def run_update(opts: UpdateOptions) -> dict[str, Any]:
    """Run the incremental MODIS update for the resolved year windows.

    Returns a per-year summary. Raises :class:`UpdateError` on any fatal
    condition (stale-lock conflict, catalogue inconsistency, unresolved tasks,
    validation failure) so the CLI can exit non-zero.
    """
    windows = resolve_windows(opts)
    if not windows:
        logger.info("Nothing to do — resolved window is empty.")
        return {"run_id": None, "status": "noop", "years": []}
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%dT%H%M%SZ") + f".{now.microsecond:06d}"
    summary: dict[str, Any] = {"run_id": run_id, "status": "ok", "years": []}
    for window in windows:
        summary["years"].append(_run_year(opts, window, run_id))
    return summary


def _run_year(opts: UpdateOptions, window: YearWindow, run_id: str) -> dict[str, Any]:
    year = window.year
    db = tracker_path(opts, year)
    started_at = _now_iso()
    summary: dict[str, Any] = {
        "year": year,
        "window": [window.start.isoformat(), window.end.isoformat()],
        "kind": window.kind,
    }
    manifest: Path | None = None
    backed_up = False
    try:
        with YearLock(opts, year):
            modis_group = _modis_group(opts, year)
            prefilled = modis_group is not None and group_is_prefilled(modis_group)
            archive_dates, axis = read_archive_dates(opts, year, modis_group)
            print(
                f"[modis {year} {window.kind}] window {window.start} → {window.end} "
                f"· archive {archive_root(opts, year)} · tracker {db} · axis {len(axis)} date(s)"
            )
            if prefilled:
                logger.info(
                    "[modis {}] prefilled year: the time axis contains every day by construction, "
                    "so the DONE-but-missing requeue heuristic is inert",
                    year,
                )
            if opts.dry_run:
                return {**summary, "dry_run": True}

            init_db(db)
            df, checksum = refresh_catalogue(opts, year, window.start, window.end, run_id)
            window_df = df[(df["date"] >= window.start.isoformat()) & (df["date"] <= window.end.isoformat())]
            tasks = to_tasks(window_df)
            tracker_rows = read_tracker(db)
            if not opts.retry_failed:
                failed_ids = {tid for tid, status in tracker_rows.items() if status == "FAILED"}
                skipped = [t for t in tasks if t["task_id"] in failed_ids]
                tasks = [t for t in tasks if t["task_id"] not in failed_ids]
                if skipped:
                    print(f"[modis {year}] --no-retry-failed: leaving {len(skipped)} FAILED task(s) unretried")

            expected_dates = {_to_date(t["date"]) for t in tasks}
            check_holes(opts, window, expected_dates, archive_dates)

            year_dates = {_to_date(d) for d in pd.to_datetime(df["date"]).dt.date}
            report = reconcile_window(tasks, tracker_rows, archive_dates, year_dates, db, prefilled=prefilled)
            print(
                f"[modis {year}] expected {report.expected} · DONE {report.done} · FAILED "
                f"{report.failed} · missing {report.pending} · requeued {report.requeued} "
                f"· orphans {len(report.orphan_dates)}"
            )

            final: dict[str, int] = {"total": 0, "DONE": 0, "FAILED": 0}
            if tasks and not opts.dry_run:
                _probe_pending_download(tasks, db)
                final = run_window_batch(opts, year, tasks, db)

            validate_year(opts, window, tasks, db)
            done_ids = {tid for tid, status in read_tracker(db).items() if status == "DONE"}
            check_samples(opts, year, sample_tasks(tasks, done_ids))
            watermark = contiguous_complete_end(df, done_ids, window.start)

            _, axis = read_archive_dates(opts, year)
            axis_sorted = not unsorted_spans(axis)
            manifest = write_manifest(
                opts,
                window,
                run_id,
                df=df,
                checksum=checksum,
                final=final,
                watermark=watermark,
                axis_sorted=axis_sorted,
                orphans=report.orphan_dates,
                status="ok",
                started_at=started_at,
            )
            summary.update(
                {
                    "status": "ok",
                    "watermark": watermark.isoformat() if watermark else None,
                    "final": final,
                    "checksum": checksum,
                    "time_axis_sorted": axis_sorted,
                }
            )
    except Exception as exc:  # noqa: BLE001 - record the failure manifest, then re-raise
        manifest = _write_failure_manifest(opts, window, run_id, df=None, started_at=started_at, exc=exc)
        summary["status"] = "failed"
        raise
    finally:
        backed_up = backup_state(opts, year, manifest=manifest)
    if not backed_up:
        raise UpdateError(f"state backup failed for year {year}")
    return summary


def _write_failure_manifest(
    opts: UpdateOptions,
    window: YearWindow,
    run_id: str,
    *,
    df: pd.DataFrame | None,
    started_at: str,
    exc: Exception | None,
) -> Path | None:
    try:
        final = stats(tracker_path(opts, window.year))
        return write_manifest(
            opts,
            window,
            run_id,
            df=df,
            checksum=None,
            final=final,
            watermark=None,
            axis_sorted=False,
            orphans=[],
            status="failed",
            started_at=started_at,
            failed=repr(exc) if exc else None,
        )
    except Exception as inner:  # noqa: BLE001 - failure manifest is best-effort
        logger.error("Could not write failure manifest: {}", inner)
        return None


# ── Status ───────────────────────────────────────────────────────────────────


def seed_tracker(opts: UpdateOptions, year: int, *, dry_run: bool = False) -> dict[str, Any]:
    """Build a year's tracker from the archive itself (no data re-reading).

    The archive is a tile mosaic, so per-task completion cannot be recovered
    from pixel data — but a date's presence on the time axis proves at least
    one tile of it was written. Seeding marks every catalogue task whose date
    is on the axis as ``DONE`` (satisfying invariant 3 by construction) and
    leaves catalogue dates missing from the axis pending, so the next update
    run processes only genuinely missing work. Idempotent: existing task rows
    are never overwritten.

    On a prefilled year (``atlantis_time_prefill`` marker) axis dates are **not**
    evidence of data — the axis holds every day by construction — so seeding is
    refused: re-run the (resume-safe) cube build to rebuild the tracker, or use
    the tracker from the original build.

    Raises:
        UpdateError: When the year has no catalogue, no archive ``modis`` group,
            or the group is prefilled.
    """
    db = tracker_path(opts, year)
    init_db(db)
    df = _load_year_catalogue(opts, year)
    if df is None:
        raise UpdateError(f"no catalogue found for {year}")
    group = _modis_group(opts, year)
    archive_dates, axis = read_archive_dates(opts, year, group)
    if not axis:
        raise UpdateError(f"archive has no modis group for {year}")
    if group is not None and group_is_prefilled(group):
        raise UpdateError(
            f"cannot seed tracker for {year}: the modis time axis is prefilled "
            "(atlantis_time_prefill) — axis dates are not evidence of data; "
            "re-run the (resume-safe) cube build to rebuild the tracker, or use "
            "the tracker from the original build"
        )

    dates = pd.to_datetime(df["date"]).dt.date
    seed_rows = df[dates.isin(set(axis))]
    pending_dates = sorted(set(dates[~dates.isin(set(axis))]))
    if not dry_run:
        now = _now_iso()
        with sqlite3.connect(db) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO tasks (task_id, status, output_uri, attempts, finished_at) "
                "VALUES (?, 'DONE', ?, 1, ?)",
                [(tid, f"archive#{tid}", now) for tid in seed_rows["task_id"]],
            )
            conn.commit()
    return {
        "year": year,
        "seeded": int(len(seed_rows)),
        "axis_dates": len(axis),
        "pending_dates": pending_dates,
        "dry_run": dry_run,
        "tracker": str(db),
    }


def status_report(opts: UpdateOptions, year: int) -> dict[str, Any]:
    """Read-only per-year status snapshot for the ``status`` command."""
    db = tracker_path(opts, year)
    report: dict[str, Any] = {"year": year, "tracker": str(db), "tracker_exists": db.exists()}
    if db.exists():
        report.update(stats(db))
        with sqlite3.connect(db) as conn:
            report["recent_failed"] = conn.execute(
                "SELECT task_id, error, attempts, finished_at FROM tasks "
                "WHERE status = 'FAILED' ORDER BY finished_at DESC LIMIT 5"
            ).fetchall()
    else:
        report.update({"DONE": 0, "FAILED": 0, "total": 0, "recent_failed": []})

    lock = lock_path(opts, year)
    report["lock"] = None if not lock.exists() else YearLock.describe(lock)

    df = _load_year_catalogue(opts, year)
    report["catalogue_rows"] = None if df is None else int(len(df))
    report["watermark"] = last_complete_for_year(opts, year)

    group = _modis_group(opts, year)
    archive_dates, axis = read_archive_dates(opts, year, group)
    prefilled = group is not None and group_is_prefilled(group)
    report["prefilled_year"] = prefilled
    if df is not None:
        tracker_rows = read_tracker(db) if db.exists() else {}
        report["date_states"] = date_states(df, tracker_rows)
        report["state_counts"], report["state_ranges"] = state_summary(df, tracker_rows, year)
        report["expected_tasks"] = int(len(df))
        done_ids = {tid for tid, status in tracker_rows.items() if status == "DONE"}
        report["pending_tasks"] = int(len(set(df["task_id"]) - done_ids))
        expected_dates = set(pd.to_datetime(df["date"]).dt.date)
        if prefilled:
            # On a prefilled year the axis always contains every date, so
            # missing work must come from the tracker: dates whose expected
            # tasks are not all DONE/FAILED.
            missing_dates = [d for d, state in report["date_states"].items() if state == "pending"]
            report["missing_ranges"] = date_ranges(sorted(missing_dates))
        else:
            report["missing_ranges"] = date_ranges(sorted(expected_dates - archive_dates))
    else:
        report["date_states"] = {}
        report["state_counts"] = {}
        report["state_ranges"] = {}
        report["expected_tasks"] = 0
        report["pending_tasks"] = 0
        report["missing_ranges"] = []

    report["archive_dates"] = len(archive_dates)
    report["time_axis_sorted"] = not unsorted_spans(axis)
    if axis:
        report["archive_first"], report["archive_last"] = axis[0], axis[-1]

    manifests = sorted(manifests_dir(opts, year).glob("*.json")) if manifests_dir(opts, year).exists() else []
    report["last_manifest"] = None
    for manifest_path in reversed(manifests):
        try:
            report["last_manifest"] = json.loads(manifest_path.read_text())
            break
        except Exception:  # noqa: BLE001 - skip unreadable manifests
            continue
    return report


# ── tmux launcher ────────────────────────────────────────────────────────────


def build_worker_command(opts: UpdateOptions, run_id: str) -> list[str]:
    """The exact ``_run-update`` argv the tmux session will execute."""
    args = ["python", "-m", "atlantis.cli", "archive", "modis", "_run-update"]
    if opts.year is not None:
        args += ["--year", str(opts.year)]
    if opts.start is not None:
        args += ["--start", opts.start.isoformat()]
    if opts.end is not None:
        args += ["--end", opts.end.isoformat()]
    args += [
        "--lookback-days",
        str(opts.lookback_days),
        "--availability-lag-days",
        str(opts.availability_lag_days),
        "--archive-base",
        opts.archive_base,
        "--state-root",
        str(opts.state_root),
        "--catalogue-base",
        opts.catalogue_base,
        "--backup-base",
        opts.backup_base,
        "--workers-min",
        str(opts.workers_min),
        "--workers-max",
        str(opts.workers_max),
        "--memory-limit",
        opts.memory_limit,
        "--dashboard-port",
        str(opts.dashboard_port),
        "--retries",
        str(opts.retries),
        "--log-every",
        str(opts.log_every),
        "--retry-failed" if opts.retry_failed else "--no-retry-failed",
    ]
    if opts.dry_run:
        args.append("--dry-run")
    return args


def launch_tmux_update(
    opts: UpdateOptions,
    *,
    run_id: str,
    repo_root: Path,
    session_name: str | None = None,
) -> tuple[str, Path, str]:
    """Start the worker in a new detached tmux session.

    Returns ``(session name, log path, worker command)``. Fails clearly when
    tmux is unavailable or the session already exists — never falls back to a
    background shell process.
    """
    if shutil.which("tmux") is None:
        raise UpdateError("tmux is not installed — use --foreground (CI/schedulers/tests)")
    worker = build_worker_command(opts, run_id)
    windows = resolve_windows(opts)
    first_year = opts.year if opts.year is not None else (windows[0].year if windows else date.today().year)
    name = session_name or f"atlantis-modis-update-{first_year}-{run_id}"
    log_path = logs_dir(opts, first_year) / f"{run_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    existing = subprocess.run(["tmux", "has-session", "-t", name], capture_output=True, check=False)
    if existing.returncode == 0:
        raise UpdateError(f"tmux session {name!r} already exists")

    command = f"cd {repo_root} && PYTHONPATH=src pixi run -e batch {' '.join(worker)} > {log_path} 2>&1"
    subprocess.run(["tmux", "new-session", "-d", "-s", name, command], check=True)
    return name, log_path, command


def _expected_by_date(df: pd.DataFrame) -> dict[date, set[str]]:
    """``{date: set of expected task ids}`` for the catalogue rows, sorted by date."""
    out: dict[date, set[str]] = {}
    for d, task_id in zip(pd.to_datetime(df["date"]).dt.date, df["task_id"]):
        out.setdefault(d, set()).add(task_id)
    return dict(sorted(out.items()))


def date_states(df: pd.DataFrame, tracker_rows: dict[str, str]) -> dict[date, str]:
    """Per-date completion state for a year's catalogue: ``done`` / ``failed`` / ``pending``.

    A date is ``done`` when every expected task is ``DONE``, ``failed`` when
    any expected task is ``FAILED`` (even if others are done), else ``pending``.
    Dates with no catalogue coverage are absent from the result.
    """
    done_ids = {tid for tid, status in tracker_rows.items() if status == "DONE"}
    states: dict[date, str] = {}
    for day, ids in _expected_by_date(df).items():
        if ids <= done_ids:
            states[day] = "done"
        elif any(tracker_rows.get(tid) == "FAILED" for tid in ids):
            states[day] = "failed"
        else:
            states[day] = "pending"
    return states


def date_ranges(dates: list[date]) -> list[tuple[date, date]]:
    """Group sorted dates into contiguous inclusive ranges, e.g. gaps to report."""
    ranges: list[tuple[date, date]] = []
    for d in sorted(dates):
        if ranges and (d - ranges[-1][1]).days == 1:
            ranges[-1] = (ranges[-1][0], d)
        else:
            ranges.append((d, d))
    return ranges


def state_summary(
    df: pd.DataFrame, tracker_rows: dict[str, str], year: int
) -> tuple[dict[str, int], dict[str, list[tuple[date, date]]]]:
    """Per-state day counts and contiguous ranges for every calendar day of *year*.

    States: ``done`` (every expected task DONE), ``failed`` (any expected task
    FAILED), ``pending`` (expected but incomplete), ``empty`` (no catalogue
    coverage that day).

    Returns:
        ``(counts, ranges)`` keyed by state.
    """
    states = date_states(df, tracker_rows)
    days_by_state: dict[str, list[date]] = {"done": [], "failed": [], "pending": [], "empty": []}
    day = date(year, 1, 1)
    while day.year == year:
        days_by_state[states.get(day, "empty")].append(day)
        day += timedelta(days=1)
    counts = {state: len(days) for state, days in days_by_state.items()}
    ranges = {state: date_ranges(days) for state, days in days_by_state.items()}
    return counts, ranges
