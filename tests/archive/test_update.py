"""Tests for the incremental MODIS archive update orchestration."""

import hashlib
import json
import os
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from atlantis.archive import datacube, grid
from atlantis.archive.cube_batch import _payload_to_dataset
from atlantis.archive.ordering import OrderedConsume, unsorted_spans
from atlantis.archive.reindex_time import reindex_group_time
from atlantis.archive.update import (
    MODIS_VAR_NAMES,
    UpdateError,
    UpdateOptions,
    YearLock,
    _modis_group,
    archive_root,
    build_worker_command,
    catalogue_uri,
    check_holes,
    contiguous_complete_end,
    group_is_prefilled,
    launch_tmux_update,
    local_catalogue_path,
    read_archive_dates,
    read_tracker,
    reconcile_window,
    refresh_catalogue,
    resolve_windows,
    run_update,
    status_report,
    tracker_path,
)
from atlantis.batch.tracker import init_db, mark_done, mark_failed, requeue, stats
from atlantis.fetchers.modis.processor import tile_bounds_from_hv


def _opts(tmp_path, **kw) -> UpdateOptions:
    defaults = dict(
        state_root=tmp_path / "state",
        archive_base=str(tmp_path / "zarr"),
        catalogue_base=str(tmp_path / "assets"),
        backup_base=str(tmp_path / "backup"),
    )
    defaults.update(kw)
    return UpdateOptions(**defaults)


def _catalogue_df(rows):
    return pd.DataFrame(rows, columns=["date", "h", "v", "task_id", "source_uri"])


def _tile_rows(days, tiles, year=2026):
    """Catalogue rows for *days* (January day numbers) of *year*."""
    rows = []
    for d in days:
        ds = f"{year}-01-{d:02d}"
        for h, v in tiles:
            task_id = f"modis-{ds.replace('-', '')}-h{h:02d}v{v:02d}"
            rows.append(
                {"date": ds, "h": h, "v": v, "task_id": task_id, "source_uri": f"https://laads/{ds}/{task_id}.hdf"}
            )
    return rows


class FakeCatalogueBuilder:
    """Builds a parquet catalogue for a window from a fixed tile set (no network)."""

    def __init__(self, year, tiles, dates=None, fail=False):
        self.year = year
        self.tiles = tiles
        self.dates = dates
        self.fail = fail
        self.calls = []

    def __call__(self, start, end, output, on_progress=None):
        self.calls.append((start, end))
        if self.fail:
            raise RuntimeError("LAADS listing failed")
        if self.dates is None:
            s, e = date.fromisoformat(start), date.fromisoformat(end)
            days = [s + timedelta(days=i) for i in range((e - s).days + 1)]
        else:
            days = self.dates
        rows = []
        for d in days:
            rows.extend(_tile_rows([d.day], self.tiles, year=d.year) if d.year == self.year else [])
        _catalogue_df(rows).to_parquet(output, index=False)
        return output


def _payload_for(task, value=50):
    west, south, east, north = tile_bounds_from_hv(int(task["h"]), int(task["v"]))
    window = grid.bounds_to_window(west, south, east, north)
    y = grid.global_y_coords()[window.row_start : window.row_stop]
    x = grid.global_x_coords()[window.col_start : window.col_stop]
    shape = (len(y), len(x))
    return {
        "task_id": task["task_id"],
        "date": task["date"],
        "h": int(task["h"]),
        "v": int(task["v"]),
        "water_fraction": np.full(shape, value, dtype="uint8"),
        "exclusion_mask": np.zeros(shape, dtype="uint8"),
        "reference_water": np.zeros(shape, dtype="uint8"),
        "recurring_flood": np.zeros(shape, dtype="uint8"),
        "y": y,
        "x": x,
    }


def _fake_run_cube_batch(produce_fn, fail_task_ids=()):
    """Engine stand-in: tracker skip/retry, scrambled completion order."""

    def run(tasks, _produce, consume, cfg):
        from atlantis.batch.tracker import get_pending, mark_done, mark_failed, stats

        init_db(cfg.db_path)
        pending = [t for t in tasks if t["task_id"] in get_pending(cfg.db_path, {t["task_id"] for t in tasks})]
        for t in reversed(pending):  # reverse completion order exercises the ordered writer
            try:
                if t["task_id"] in fail_task_ids:
                    raise RuntimeError("simulated failure")
                payload = produce_fn(t)
                consume(payload)
                mark_done(cfg.db_path, payload["task_id"], f"archive#{payload['task_id']}")
            except Exception as exc:  # noqa: BLE001 - mirror the engine's per-task handling
                mark_failed(cfg.db_path, t["task_id"], repr(exc), attempts=1)
        return stats(cfg.db_path)

    return run


def _build_legacy_axis(opts, year, dates, tiles=None):
    """Write *dates* through a non-prefilled writer session (legacy-style archive)."""
    from atlantis.archive.writer import ArchiveWriter

    tiles = tiles or [(10, 3)]
    writer = ArchiveWriter(archive_root(opts, year))
    with writer.session("modis", list(MODIS_VAR_NAMES)) as session:
        for d in dates:
            for h, v in tiles:
                task = {"task_id": f"modis-{d:%Y%m%d}-h{h:02d}v{v:02d}", "date": d.isoformat(), "h": h, "v": v}
                session.write(_payload_to_dataset(_payload_for(task)), time=d)


def _any_year_builder(start, end, output, on_progress=None):
    """Catalogue builder covering every date in the window, any year."""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    rows = []
    for i in range((e - s).days + 1):
        d = s + timedelta(days=i)
        for h, v in ((10, 3), (11, 3)):
            ds = d.isoformat()
            rows.append(
                {
                    "date": ds,
                    "h": h,
                    "v": v,
                    "task_id": f"modis-{ds.replace('-', '')}-h{h:02d}v{v:02d}",
                    "source_uri": f"https://laads/{ds}/x.hdf",
                }
            )
    _catalogue_df(rows).to_parquet(output, index=False)


# ── Small units ──────────────────────────────────────────────────────────────


class TestRoutingAndWindows:
    def test_archive_root_per_year(self, tmp_path):
        opts = _opts(tmp_path)
        assert archive_root(opts, 2025) == f"{tmp_path}/zarr/2025"
        assert catalogue_uri(opts, 2025) == f"{tmp_path}/assets/modis_archive_catalog_2025.parquet"
        assert tracker_path(opts, 2025) == tmp_path / "state" / "2025" / "cube_tracker.db"

    def test_window_split_across_year_boundary(self, tmp_path):
        opts = _opts(tmp_path, start=date(2025, 12, 30), end=date(2026, 1, 2))
        windows = resolve_windows(opts)
        assert [(w.year, w.start, w.end) for w in windows] == [
            (2025, date(2025, 12, 30), date(2025, 12, 31)),
            (2026, date(2026, 1, 1), date(2026, 1, 2)),
        ]

    def test_window_year_restriction(self, tmp_path):
        opts = _opts(tmp_path, year=2025, start=date(2025, 12, 30), end=date(2026, 1, 2))
        windows = resolve_windows(opts)
        assert [(w.year, w.start, w.end) for w in windows] == [(2025, date(2025, 12, 30), date(2025, 12, 31))]

    def test_weekly_window_from_watermark(self, tmp_path, capsys):
        opts = _opts(tmp_path, year=2026, lookback_days=14, today=date(2026, 8, 2))
        db = tracker_path(opts, 2026)
        init_db(db)
        df = _catalogue_df(_tile_rows([1, 2], [(10, 3)]))
        local = local_catalogue_path(opts, 2026)
        local.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(local, index=False)
        for tid in df["task_id"]:
            mark_done(db, tid, "x")
        windows = resolve_windows(opts)
        assert len(windows) == 1
        w = windows[0]
        assert w.start == date(2026, 6, 26)  # stale year: capped to the newest 31 days
        assert w.end == date(2026, 7, 26)  # today - lag
        assert w.kind == "weekly"
        assert "guardrail" in capsys.readouterr().out

    def test_catchup_capped_to_newest_month(self, tmp_path, capsys):
        """A >1-month backlog on an auto window processes only the newest 31 days."""
        opts = _opts(tmp_path, year=2026, today=date(2026, 8, 12))  # no tracker, no catalogue
        (windows,) = resolve_windows(opts)
        assert (windows.start, windows.end) == (date(2026, 7, 6), date(2026, 8, 5))
        assert "guardrail" in capsys.readouterr().out

    def test_explicit_windows_never_capped(self, tmp_path, capsys):
        """--start/--end backfill windows are deliberate: no guardrail."""
        opts = _opts(tmp_path, start=date(2026, 1, 1), end=date(2026, 8, 5))
        (windows,) = resolve_windows(opts)
        assert (windows.start, windows.end) == (date(2026, 1, 1), date(2026, 8, 5))
        assert "guardrail" not in capsys.readouterr().out

    def test_rollover_reaches_previous_year(self, tmp_path, capsys):
        """A fresh year reaches back: the December backlog and January both run."""
        opts = _opts(tmp_path, today=date(2027, 1, 12))  # no trackers anywhere
        windows = resolve_windows(opts)
        assert [(w.year, w.start, w.end) for w in windows] == [
            (2026, date(2026, 12, 6), date(2026, 12, 31)),
            (2027, date(2027, 1, 1), date(2027, 1, 5)),
        ]
        assert "guardrail" in capsys.readouterr().out


class TestReconcile:
    @staticmethod
    def _tasks(days, tiles, year=2026):
        return [
            {"task_id": f"modis-{year}{d:02d}01-h{h:02d}v{v:02d}", "date": f"{year}-01-{d:02d}", "h": h, "v": v}
            for d in days
            for h, v in tiles
        ]

    def test_classifies_done_failed_pending(self, tmp_path):
        tasks = self._tasks([1, 2, 3], [(10, 3)])
        done = {t["task_id"] for t in tasks[:2]}
        failed = {tasks[-1]["task_id"]}
        tracker = {tid: ("DONE" if tid in done else "FAILED") for tid in done | failed}
        archive_dates = {date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)}
        rep = reconcile_window(tasks, tracker, archive_dates, archive_dates, tmp_path / "db", warn=lambda _m: None)
        assert (rep.expected, rep.done, rep.failed) == (3, 2, 1)
        assert rep.pending == 0

    def test_missing_date_is_a_gap_not_max_date(self, tmp_path):
        tasks = self._tasks([1, 2, 3], [(10, 3)])
        tracker = {t["task_id"]: "DONE" for t in tasks if t["date"] != "2026-01-02"}
        archive_dates = {date(2026, 1, 1), date(2026, 1, 3)}  # max date present
        rep = reconcile_window(tasks, tracker, archive_dates, archive_dates, tmp_path / "db", warn=lambda _m: None)
        assert rep.pending == 1

    def test_done_but_missing_from_archive_is_requeued(self, tmp_path):
        tasks = self._tasks([1, 2], [(10, 3)])
        db = tmp_path / "tracker.db"
        init_db(db)
        for t in tasks:
            mark_done(db, t["task_id"], "x")
        warnings = []
        rep = reconcile_window(
            tasks,
            read_tracker(db),
            {date(2026, 1, 1)},
            {date(2026, 1, 1), date(2026, 1, 2)},
            db,
            warn=warnings.append,
        )
        assert rep.requeued == 1
        assert warnings and "requeueing" in warnings[0]
        assert read_tracker(db) == {tasks[0]["task_id"]: "DONE"}  # only the date-2 row deleted

    def test_requeue_helper(self, tmp_path):
        db = tmp_path / "tracker.db"
        init_db(db)
        mark_done(db, "modis-20260101-h10v03", "x")
        requeue(db, "modis-20260101-h10v03")
        assert "modis-20260101-h10v03" not in read_tracker(db)

    def test_orphan_dates_reported(self, tmp_path):
        tasks = self._tasks([1], [(10, 3)])
        rep = reconcile_window(
            tasks,
            {},
            {date(2026, 1, 1), date(2026, 5, 5)},
            {date(2026, 1, 1)},
            tmp_path / "db",
            warn=lambda _m: None,
        )
        assert rep.orphan_dates == [date(2026, 5, 5)]

    def test_prefilled_year_reports_no_phantom_orphans(self, tmp_path):
        """A prefilled axis holds every day by construction — no orphan spam."""
        tasks = self._tasks([1], [(10, 3)])
        full_year = {date(2026, 1, 1) + timedelta(days=i) for i in range(365)}
        rep = reconcile_window(
            tasks,
            {},
            full_year,
            {date(2026, 1, 1)},
            tmp_path / "db",
            warn=lambda _m: None,
            prefilled=True,
        )
        assert rep.orphan_dates == []


class TestPreflightProbe:
    """Preflight tile-download probe: fail fast before the batch launches."""

    @staticmethod
    def _tasks():
        return [
            {"task_id": "t1", "date": "2026-08-05", "h": 0, "v": 0, "source_uri": "https://example/one.hdf"},
            {"task_id": "t2", "date": "2026-08-05", "h": 0, "v": 1, "source_uri": "https://example/two.hdf"},
        ]

    def test_skipped_when_all_done(self, tmp_path, monkeypatch):
        from atlantis.archive import update as upd
        from atlantis.batch.tracker import init_db, mark_done

        db = tmp_path / "cube_tracker.db"
        init_db(db)
        mark_done(db, "t1", "archive#t1")
        mark_done(db, "t2", "archive#t2")
        called: list[str] = []
        monkeypatch.setattr(upd, "probe_download", lambda url: called.append(url))
        upd._probe_pending_download(self._tasks(), db)
        assert called == []

    def test_probes_first_pending_and_failed_tile(self, tmp_path, monkeypatch):
        from atlantis.archive import update as upd
        from atlantis.batch.tracker import init_db, mark_done

        db = tmp_path / "cube_tracker.db"
        init_db(db)
        mark_done(db, "t1", "archive#t1")
        called: list[str] = []
        monkeypatch.setattr(upd, "probe_download", lambda url: called.append(url))
        upd._probe_pending_download(self._tasks(), db)
        assert called == ["https://example/two.hdf"]

    def test_probe_failure_becomes_update_error(self, tmp_path, monkeypatch):
        from atlantis.archive.update import UpdateError, _probe_pending_download
        from atlantis.batch.tracker import init_db, mark_failed
        from atlantis.utils.io import DownloadContentError

        db = tmp_path / "cube_tracker.db"
        init_db(db)
        mark_failed(db, "t1", "stale failure", 3)
        monkeypatch.setattr("atlantis.archive.update.time.sleep", lambda _s: None)

        def _boom(url):
            raise DownloadContentError("LAADS rejected the token")

        monkeypatch.setattr("atlantis.archive.update.probe_download", _boom)
        with pytest.raises(UpdateError, match="LAADS rejected the token"):
            _probe_pending_download(self._tasks(), db)

    def test_probe_http_401_becomes_update_error(self, tmp_path, monkeypatch):
        from requests import HTTPError

        from atlantis.archive.update import UpdateError, _probe_pending_download
        from atlantis.batch.tracker import init_db

        db = tmp_path / "cube_tracker.db"
        init_db(db)

        class _Resp:
            status_code = 401

        def _unauthorized(url):
            raise HTTPError("401 error", response=_Resp())

        monkeypatch.setattr("atlantis.archive.update.probe_download", _unauthorized)
        with pytest.raises(UpdateError, match="rejected the download token"):
            _probe_pending_download(self._tasks(), db)

    def test_probe_retries_then_succeeds(self, tmp_path, monkeypatch):
        from requests import ConnectionError

        from atlantis.archive.update import _probe_pending_download
        from atlantis.batch.tracker import init_db

        db = tmp_path / "cube_tracker.db"
        init_db(db)
        monkeypatch.setattr("atlantis.archive.update.time.sleep", lambda _s: None)
        calls: list[str] = []

        def _flaky(url):
            calls.append(url)
            if len(calls) == 1:
                raise ConnectionError("first attempt dropped")
            return None

        monkeypatch.setattr("atlantis.archive.update.probe_download", _flaky)
        _probe_pending_download(self._tasks(), db)
        assert calls == ["https://example/one.hdf", "https://example/one.hdf"]

    def test_persistent_transient_failure_warns_and_continues(self, tmp_path, monkeypatch):
        from requests import ConnectionError

        from atlantis.archive.update import _probe_pending_download
        from atlantis.batch.tracker import init_db

        db = tmp_path / "cube_tracker.db"
        init_db(db)
        monkeypatch.setattr("atlantis.archive.update.time.sleep", lambda _s: None)
        calls: list[str] = []

        def _down(url):
            calls.append(url)
            raise ConnectionError("network down")

        monkeypatch.setattr("atlantis.archive.update.probe_download", _down)
        _probe_pending_download(self._tasks(), db)
        assert len(calls) == 3  # retried, then warned and continued


class TestWatermark:
    def test_contiguous_only(self):
        df = _catalogue_df(_tile_rows([1, 2, 3, 4], [(10, 3)]))
        done = {"modis-20260101-h10v03", "modis-20260102-h10v03", "modis-20260104-h10v03"}
        # later date complete, but 03 is a gap → watermark must not skip it
        assert contiguous_complete_end(df, done, date(2026, 1, 1)) == date(2026, 1, 2)

    def test_all_complete(self):
        df = _catalogue_df(_tile_rows([1, 2, 3], [(10, 3)]))
        done = set(df["task_id"])
        assert contiguous_complete_end(df, done, date(2026, 1, 1)) == date(2026, 1, 3)


class TestDateStates:
    def test_done_failed_pending(self):
        from atlantis.archive.update import date_states

        df = _catalogue_df(_tile_rows([1, 2, 3], [(10, 3)]))
        done = {"modis-20260101-h10v03"}
        failed = {"modis-20260102-h10v03"}
        tracker = {tid: "DONE" if tid in done else "FAILED" for tid in done | failed}
        states = date_states(df, tracker)
        assert states[date(2026, 1, 1)] == "done"
        assert states[date(2026, 1, 2)] == "failed"
        assert states[date(2026, 1, 3)] == "pending"

    def test_failed_wins_over_done_same_date(self):
        from atlantis.archive.update import date_states

        df = _catalogue_df(_tile_rows([2], [(10, 3), (11, 3)]))
        tracker = {"modis-20260102-h10v03": "DONE", "modis-20260102-h11v03": "FAILED"}
        assert date_states(df, tracker)[date(2026, 1, 2)] == "failed"

    def test_no_catalogue_coverage_is_absent(self):
        from atlantis.archive.update import date_states

        df = _catalogue_df(_tile_rows([1], [(10, 3)]))
        assert date_states(df, {}) == {date(2026, 1, 1): "pending"}

    def test_state_summary_counts_and_ranges(self):
        from atlantis.archive.update import state_summary

        df = _catalogue_df(_tile_rows([1, 2, 3], [(10, 3), (11, 3)]))
        tracker = {
            "modis-20260101-h10v03": "DONE",
            "modis-20260101-h11v03": "DONE",
            "modis-20260102-h10v03": "DONE",
            "modis-20260102-h11v03": "FAILED",
        }
        counts, ranges = state_summary(df, tracker, 2026)
        assert counts == {"done": 1, "failed": 1, "pending": 1, "empty": 362}
        assert ranges["done"] == [(date(2026, 1, 1), date(2026, 1, 1))]
        assert ranges["failed"] == [(date(2026, 1, 2), date(2026, 1, 2))]
        assert ranges["pending"] == [(date(2026, 1, 3), date(2026, 1, 3))]
        assert ranges["empty"] == [(date(2026, 1, 4), date(2026, 12, 31))]


class TestDateRanges:
    def test_contiguous_ranges(self):
        from atlantis.archive.update import date_ranges

        days = [date(2026, 1, d) for d in (1, 2, 3, 5, 7, 8)]
        assert date_ranges(days) == [
            (date(2026, 1, 1), date(2026, 1, 3)),
            (date(2026, 1, 5), date(2026, 1, 5)),
            (date(2026, 1, 7), date(2026, 1, 8)),
        ]

    def test_heatmap_renders_one_cell_per_day(self):
        from atlantis.cli import _render_year_heatmap

        states = {date(2026, 1, d): "done" for d in range(1, 32)}
        text = _render_year_heatmap(states, 2026)
        assert text.plain.count("\n") == 12  # one row per month
        assert "Jan" in text.plain and "Dec" in text.plain
        assert len(text.spans) == 12 + 365  # month labels + one cell per day (2026: 365 days)

    def test_heatmap_glyphs_distinguish_states(self):
        """Distinct glyphs keep the grid readable in monochrome terminals."""
        from atlantis.cli import _render_year_heatmap

        states = {
            date(2026, 1, 1): "done",
            date(2026, 1, 2): "failed",
            date(2026, 1, 3): "pending",
            date(2026, 1, 4): "empty",
        }
        jan = _render_year_heatmap(states, 2026).plain.splitlines()[0]
        assert jan[4] == "#" and jan[6] == "x" and jan[8] == "o" and jan[10] == "."

    def test_monthly_overview_dominant_state(self):
        from atlantis.cli import _render_year_overview

        states = {
            **{date(2026, 1, d): "done" for d in range(1, 31)},  # Jan fully complete
            **{date(2026, 2, d): "done" for d in range(1, 15)},
            date(2026, 2, 16): "failed",  # Feb has a failed day → failed dominates
            date(2026, 3, 1): "pending",  # Mar has pending work
            # Apr onward: no coverage
        }
        text = _render_year_overview(states, 2026)
        assert text.plain.startswith("2026")
        assert len(text.plain) == 4 + 2 + 12 * 2  # year + gap + one 2-char block per month
        assert [sp.style for sp in text.spans] == ["green", "red", "yellow"] + ["bright_black"] * 9

    def test_status_all_years(self, tmp_path):
        from typer.testing import CliRunner

        import atlantis.cli
        from atlantis.batch.tracker import init_db

        state = tmp_path / "state"
        for y in (2025, 2026):
            year_dir = state / str(y)
            year_dir.mkdir(parents=True)
            init_db(year_dir / "cube_tracker.db")
        result = CliRunner().invoke(
            atlantis.cli.cli,
            [
                "archive",
                "modis",
                "status",
                "--state-root",
                str(state),
                "--archive-base",
                str(tmp_path / "zarr"),
                "--catalogue-base",
                str(tmp_path / "assets"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "MODIS archive — all years" in result.output
        assert "2025" in result.output and "2026" in result.output
        assert "Monthly overview" in result.output
        assert "Pass --year" in result.output


class TestCatalogueRefresh:
    def test_merge_dedupe_and_promote(self, tmp_path):
        opts = _opts(tmp_path)
        tiles = [(10, 3), (11, 3)]
        existing = _catalogue_df(_tile_rows([1], tiles))
        canonical = Path(catalogue_uri(opts, 2026))
        canonical.parent.mkdir(parents=True, exist_ok=True)
        existing.to_parquet(canonical, index=False)

        opts.catalogue_builder = FakeCatalogueBuilder(2026, tiles, dates=[date(2026, 1, 2)])
        df, checksum = refresh_catalogue(opts, 2026, date(2026, 1, 2), date(2026, 1, 2), "run1")

        assert len(df) == 4  # 2 dates × 2 tiles
        assert sorted(df["date"].unique()) == ["2026-01-01", "2026-01-02"]
        assert len(pd.read_parquet(canonical)) == 4
        assert checksum == hashlib.sha256(local_catalogue_path(opts, 2026).read_bytes()).hexdigest()

    def test_fresh_row_wins_on_same_tile(self, tmp_path):
        opts = _opts(tmp_path)
        old = _catalogue_df(
            [{"date": "2026-01-01", "h": 10, "v": 3, "task_id": "modis-20260101-h10v03", "source_uri": "old"}]
        )
        Path(catalogue_uri(opts, 2026)).parent.mkdir(parents=True, exist_ok=True)
        old.to_parquet(catalogue_uri(opts, 2026), index=False)

        def builder_with_new_uri(start, end, output, on_progress=None):
            _catalogue_df(
                [{"date": "2026-01-01", "h": 10, "v": 3, "task_id": "modis-20260101-h10v03", "source_uri": "new"}]
            ).to_parquet(output, index=False)

        opts.catalogue_builder = builder_with_new_uri
        df, _ = refresh_catalogue(opts, 2026, date(2026, 1, 1), date(2026, 1, 1), "run1")
        assert len(df) == 1
        assert df.iloc[0]["source_uri"] == "new"

    def test_rows_outside_year_dropped(self, tmp_path):
        opts = _opts(tmp_path)
        opts.catalogue_builder = FakeCatalogueBuilder(2026, [(10, 3)], dates=[date(2026, 1, 1), date(2027, 1, 1)])
        df, _ = refresh_catalogue(opts, 2026, date(2026, 1, 1), date(2026, 1, 1), "run1")
        assert list(df["date"].unique()) == ["2026-01-01"]

    def test_failed_build_leaves_canonical_intact(self, tmp_path):
        opts = _opts(tmp_path)
        existing = _catalogue_df(_tile_rows([1], [(10, 3)]))
        canonical = Path(catalogue_uri(opts, 2026))
        canonical.parent.mkdir(parents=True, exist_ok=True)
        existing.to_parquet(canonical, index=False)
        opts.catalogue_builder = FakeCatalogueBuilder(2026, [(10, 3)], fail=True)
        with pytest.raises(RuntimeError, match="LAADS listing failed"):
            refresh_catalogue(opts, 2026, date(2026, 1, 1), date(2026, 1, 2), "run1")
        assert len(pd.read_parquet(canonical)) == 1  # untouched

    def test_empty_catalogue_rejected(self, tmp_path):
        opts = _opts(tmp_path)
        opts.catalogue_builder = FakeCatalogueBuilder(2026, [(10, 3)], dates=[])
        with pytest.raises(UpdateError, match="empty"):
            refresh_catalogue(opts, 2026, date(2026, 1, 1), date(2026, 1, 2), "run1")


class TestLock:
    def test_live_lock_blocks(self, tmp_path):
        from datetime import datetime, timezone

        opts = _opts(tmp_path)
        lock = tmp_path / "state" / "2026" / "update.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}))
        with pytest.raises(UpdateError, match="another update"):
            with YearLock(opts, 2026):
                pass

    def test_stale_lock_reclaimed(self, tmp_path):
        opts = _opts(tmp_path)
        lock = tmp_path / "state" / "2026" / "update.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": 999999999, "started_at": "2026-07-29T00:00:00+00:00"}))
        with YearLock(opts, 2026):
            assert lock.exists()
        assert not lock.exists()

    def test_old_lock_is_stale(self, tmp_path):
        opts = _opts(tmp_path)
        lock = tmp_path / "state" / "2026" / "update.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": os.getpid(), "started_at": "2025-01-01T00:00:00+00:00"}))
        with YearLock(opts, 2026):
            pass


class TestOrderedConsume:
    class _FakeSession:
        """Records writes and marks the written date's tasks DONE (like the engine)."""

        def __init__(self, db=None, tasks=()):
            self.writes = []
            self._db = db
            self._tasks = list(tasks)

        def write(self, ds, time=None):
            self.writes.append(time)
            if self._db is not None:
                for t in self._tasks:
                    if date.fromisoformat(t["date"]) == time:
                        mark_done(self._db, t["task_id"], "x")

    @staticmethod
    def _tasks(days):
        return [{"task_id": f"modis-202601{d:02d}01-h10v03", "date": f"2026-01-{d:02d}", "h": 10, "v": 3} for d in days]

    def test_buffers_out_of_order_completions(self, tmp_path):
        db = tmp_path / "tracker.db"
        init_db(db)
        tasks = self._tasks([1, 2, 3])
        session = self._FakeSession(db, tasks)
        consumer = OrderedConsume(session, db, tasks)
        consumer.write(object(), date(2026, 1, 3))  # completions arrive in reverse order
        consumer.write(object(), date(2026, 1, 2))
        assert session.writes == []  # buffered: earlier dates unresolved
        mark_done(db, tasks[0]["task_id"], "x")
        consumer.write(object(), date(2026, 1, 1))  # resolves date 1 → flush ascending
        assert session.writes == [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]

    def test_drain_flushes_remainder_ascending(self, tmp_path):
        db = tmp_path / "tracker.db"
        init_db(db)
        consumer = OrderedConsume(self._FakeSession(), db, self._tasks([1, 2, 3]))
        consumer.write(object(), date(2026, 1, 3))
        consumer.write(object(), date(2026, 1, 1))
        consumer.drain()
        assert consumer._session.writes == [date(2026, 1, 1), date(2026, 1, 3)]

    def test_failed_date_does_not_block_later_dates(self, tmp_path):
        db = tmp_path / "tracker.db"
        init_db(db)
        tasks = self._tasks([1, 2])
        mark_failed(db, tasks[0]["task_id"], "boom", 1)
        session = self._FakeSession()
        consumer = OrderedConsume(session, db, tasks)
        consumer.write(object(), date(2026, 1, 2))
        assert session.writes == [date(2026, 1, 2)]  # date 1 resolved (FAILED) → not a blocker

    @staticmethod
    def _gfm_tasks(days):
        return [
            {
                "task_id": f"gfm-EMSR712-10-EU020M_E036N009T3-202410{d:02d}",
                "date": f"2024-10-{d:02d}",
                "equi7_tile": "EU020M_E036N009T3",
            }
            for d in days
        ]

    def test_gfm_style_task_ids(self, tmp_path):
        """Dates are resolved via the task map, not parsed out of the id."""
        db = tmp_path / "tracker.db"
        init_db(db)
        tasks = self._gfm_tasks([29, 30, 31])
        session = self._FakeSession(db, tasks)
        consumer = OrderedConsume(session, db, tasks)
        consumer.write(object(), date(2024, 10, 31))  # completions arrive in reverse order
        consumer.write(object(), date(2024, 10, 30))
        assert session.writes == []  # buffered: earlier dates unresolved
        mark_done(db, tasks[0]["task_id"], "x")
        consumer.write(object(), date(2024, 10, 29))  # resolves date 29 → flush ascending
        assert session.writes == [date(2024, 10, 29), date(2024, 10, 30), date(2024, 10, 31)]

    def test_foreign_tracker_rows_do_not_resolve_run_dates(self, tmp_path):
        """Tracker rows for tasks outside the run never count towards resolution."""
        db = tmp_path / "tracker.db"
        init_db(db)
        tasks = self._tasks([1, 2])
        mark_done(db, "modis-20260101-h10v04", "x")  # date-1 row for a different tile, not in this run
        session = self._FakeSession(db, tasks)
        consumer = OrderedConsume(session, db, tasks)
        consumer.write(object(), date(2026, 1, 2))
        assert session.writes == []  # date 1 of this run still unresolved
        mark_done(db, tasks[0]["task_id"], "x")
        consumer.write(object(), date(2026, 1, 1))
        assert session.writes == [date(2026, 1, 1), date(2026, 1, 2)]


class TestSpansAndHoles:
    def test_unsorted_spans(self):
        assert unsorted_spans([1, 2, 3]) == []
        assert unsorted_spans([1, 3, 2]) == [(1, 2)]
        assert unsorted_spans([3, 2, 1]) == [(0, 2)]
        assert unsorted_spans([1, 1, 2]) == [(0, 1)]

    def test_check_holes_refuses_earlier_missing_date(self, tmp_path):
        opts = _opts(tmp_path)
        window = resolve_windows(_opts(tmp_path, start=date(2026, 1, 1), end=date(2026, 1, 10)))[0]
        expected = {date(2026, 1, 3), date(2026, 1, 8)}
        with pytest.raises(UpdateError, match="append-only policy"):
            check_holes(opts, window, expected, {date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 8)})
        # dates beyond the axis tail append fine
        check_holes(opts, window, expected, {date(2026, 1, 1), date(2026, 1, 2)})


class TestReindex:
    def test_reindex_preserves_data_and_inserts_missing_slot(self, tmp_path):
        """The migration moves data with its date and inserts empty slots."""
        from atlantis.archive import _store
        from atlantis.archive.writer import ArchiveWriter

        root = tmp_path / "zarr" / "2026"
        writer = ArchiveWriter(root)
        with writer.session("modis", list(MODIS_VAR_NAMES)) as session:
            session.write(
                _payload_to_dataset(_payload_for({"task_id": "x1", "date": "2026-01-01", "h": 10, "v": 3}, value=10)),
                time=date(2026, 1, 1),
            )
            session.write(
                _payload_to_dataset(_payload_for({"task_id": "x3", "date": "2026-01-03", "h": 10, "v": 3}, value=30)),
                time=date(2026, 1, 3),
            )

        store = _store.store_for(str(root), "datacube.zarr", None)
        reindex_group_time(
            store,
            "modis",
            list(MODIS_VAR_NAMES),
            expected_dates=[date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)],
        )
        _, axis = read_archive_dates(_opts(tmp_path, archive_base=str(tmp_path / "zarr")), 2026)
        assert axis == [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]

        # data followed its date: slot 1 holds the 10-valued window, slot 2 is NODATA
        group = datacube.open_root(store, mode="r")["modis"]
        west, south, east, north = tile_bounds_from_hv(10, 3)
        window = grid.bounds_to_window(west, south, east, north)
        arr = group["water_fraction"]
        d1 = arr[0, window.row_start : window.row_stop, window.col_start : window.col_stop]
        d2 = arr[1, window.row_start : window.row_stop, window.col_start : window.col_stop]
        d3 = arr[2, window.row_start : window.row_stop, window.col_start : window.col_stop]
        assert int(np.max(d1)) == 10
        assert not np.any(d2 != 255)  # inserted slot is NODATA fill
        assert int(np.max(d3)) == 30

    def test_reindex_noop_when_already_sorted(self, tmp_path):
        from atlantis.archive import _store
        from atlantis.archive.writer import ArchiveWriter

        root = tmp_path / "zarr" / "2026"
        writer = ArchiveWriter(root)
        with writer.session("modis", ("water_fraction",)) as session:
            session.write(
                _payload_to_dataset(_payload_for({"task_id": "x", "date": "2026-01-01", "h": 10, "v": 3})),
                time=date(2026, 1, 1),
            )
        store = _store.store_for(str(root), "datacube.zarr", None)
        assert len(reindex_group_time(store, "modis", ("water_fraction",))) == 1
        _, axis = read_archive_dates(_opts(tmp_path, archive_base=str(tmp_path / "zarr")), 2026)
        assert axis == [date(2026, 1, 1)]
        assert not (tmp_path / "zarr" / "2026" / "datacube.zarr" / "_modis_sorted").exists()

    def test_reindex_resumes_from_complete_temp_group(self, tmp_path, monkeypatch):
        """A leftover complete temp group (e.g. after a failed swap) is reused, not re-copied."""
        from atlantis.archive import _store
        from atlantis.archive.reindex_time import reindex_group_time
        from atlantis.archive.writer import ArchiveWriter

        root = tmp_path / "zarr" / "2026"
        writer = ArchiveWriter(root)
        with writer.session("modis", ("water_fraction",)) as session:
            session.write(
                _payload_to_dataset(_payload_for({"task_id": "x1", "date": "2026-01-01", "h": 10, "v": 3}, value=10)),
                time=date(2026, 1, 1),
            )
            session.write(
                _payload_to_dataset(_payload_for({"task_id": "x3", "date": "2026-01-03", "h": 10, "v": 3}, value=30)),
                time=date(2026, 1, 3),
            )
        store = _store.store_for(str(root), "datacube.zarr", None)
        expected = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]

        # first run: swap fails (like the async-fs S3 failure) → temp group left behind
        import atlantis.archive.reindex_time as reindex_mod

        real_swap = reindex_mod._swap_group
        monkeypatch.setattr(
            "atlantis.archive.reindex_time._swap_group",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("swap failed")),
        )
        with pytest.raises(RuntimeError, match="swap failed"):
            reindex_group_time(store, "modis", ("water_fraction",), expected_dates=expected)
        monkeypatch.setattr("atlantis.archive.reindex_time._swap_group", real_swap)
        temp = root / "datacube.zarr" / "_modis_sorted"
        assert temp.exists()
        _, axis = read_archive_dates(_opts(tmp_path, archive_base=str(tmp_path / "zarr")), 2026)
        assert axis == [date(2026, 1, 1), date(2026, 1, 3)]  # archive untouched

        # second run: temp group is complete → copy is skipped (create_group must NOT be called)
        monkeypatch.setattr(
            "atlantis.archive.reindex_time.zarr.Group.create_group",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("copy path taken")),
        )
        target = reindex_group_time(store, "modis", ("water_fraction",), expected_dates=expected)
        assert len(target) == 3
        _, axis = read_archive_dates(_opts(tmp_path, archive_base=str(tmp_path / "zarr")), 2026)
        assert axis == expected
        assert not temp.exists()  # promoted into place

    def test_remote_swap_deletes_old_verified_then_copies_and_removes_temp(self, tmp_path, monkeypatch):
        """The swap deletes the old group (verified zero), copies onto the absent dest, removes temp last."""
        from atlantis.archive.reindex_time import _swap_group

        calls = []
        src = "s3://atlantis/zarr/2025/datacube.zarr/_modis_sorted"
        dst = "s3://atlantis/zarr/2025/datacube.zarr/modis"

        class _FakeFS:
            def __init__(self):
                self.dst_keys = ["a", "b", "c"]  # old group exists initially
                self.src_keys = ["a", "b", "c"]

            def find(self, path):
                calls.append(("find", path))
                if path.endswith("/_modis_sorted"):
                    return list(self.src_keys)
                return list(self.dst_keys)

            def exists(self, path):
                calls.append(("exists", path))
                return path.endswith("/modis/zarr.json") and bool(self.dst_keys)

            def copy(self, src, dst, recursive=False, on_error=None):
                calls.append(("copy", src, dst, recursive, on_error))
                if not self.dst_keys:  # only lands directly when the dest is absent
                    self.dst_keys = list(self.src_keys)

            def rm(self, path, recursive=False):
                calls.append(("rm", path, recursive))
                if path.endswith("/modis"):
                    self.dst_keys = []
                elif path.endswith("/_modis_sorted"):
                    self.src_keys = []

        class _FakeStore:
            path = "s3://atlantis/zarr/2025/datacube.zarr"

        monkeypatch.setattr(
            "atlantis.archive.reindex_time.s3fs.S3FileSystem", lambda **kw: calls.append(("fs", kw)) or _FakeFS()
        )
        _swap_group(_FakeStore(), "modis", "_modis_sorted", {"endpoint_url": "http://store"})
        assert calls[0] == ("fs", {"endpoint_url": "http://store"})
        order = [c[0] for c in calls]
        last_rm = len(order) - 1 - order[::-1].index("rm")
        assert order.index("find") < order.index("rm") < order.index("copy") < last_rm
        assert ("find", src) in calls  # source counted before anything is deleted
        assert ("rm", dst, True) in calls  # old group deleted...
        assert ("copy", src, dst, True, "raise") in calls  # ...before the copy runs
        assert calls[-1] == ("rm", src, True)  # temp removed last, only after verification
        assert ("exists", f"{dst}/zarr.json") in calls

    def test_remote_swap_aborts_when_copy_lands_nested(self, tmp_path, monkeypatch):
        """A copy that nests under the dest (zarr.json never at the right path) must fail, keep temp."""
        from atlantis.archive.reindex_time import _swap_group

        monkeypatch.setattr("atlantis.archive.reindex_time.time.sleep", lambda _s: None)
        calls = []

        class _FakeFS:
            def __init__(self):
                self.deleted = True  # old group deleted; copy then re-populates (nested)

            def find(self, path):
                calls.append(("find", path))
                if path.endswith("/_modis_sorted"):
                    return ["a", "b", "c"]
                return [] if self.deleted else ["a", "b", "c"]

            def exists(self, path):
                if path.endswith("/modis/zarr.json"):
                    return False  # zarr.json sits one level down — copy landed nested
                return path.endswith("/modis/_modis_sorted")

            def copy(self, src, dst, recursive=False, on_error=None):
                self.deleted = False  # files reappear under dst/_modis_sorted (count passes)

            def rm(self, path, recursive=False):
                if path.endswith("/_modis_sorted"):
                    raise AssertionError("rm(src) must not run - the temp group is the only good copy")
                self.deleted = True

        class _FakeStore:
            path = "s3://atlantis/zarr/2025/datacube.zarr"

        monkeypatch.setattr("atlantis.archive.reindex_time.s3fs.S3FileSystem", lambda **kw: _FakeFS())
        with pytest.raises(RuntimeError, match="could not promote"):
            _swap_group(_FakeStore(), "modis", "_modis_sorted", {})

    def test_remote_swap_self_heals_nested_attempt(self, tmp_path, monkeypatch):
        """A copy that lands nested is cleaned up; the retry copies flat onto the absent dest."""
        from atlantis.archive.reindex_time import _swap_group

        monkeypatch.setattr("atlantis.archive.reindex_time.time.sleep", lambda _s: None)
        calls = []

        class _FakeFS:
            def __init__(self):
                self.src_keys = ["a", "b", "c"]
                self.dst_keys = []
                self.nested = False
                self.copies = 0

            def find(self, path):
                calls.append(("find", path))
                if path.endswith("/_modis_sorted"):
                    return list(self.src_keys)
                if self.nested:
                    return ["nested/a", "nested/b", "nested/c"]
                return list(self.dst_keys)

            def exists(self, path):
                calls.append(("exists", path))
                if path.endswith("/modis/zarr.json"):
                    return not self.nested and bool(self.dst_keys)
                return False

            def copy(self, src, dst, recursive=False, on_error=None):
                calls.append(("copy", src, dst))
                self.copies += 1
                if self.copies == 1:
                    self.nested = True  # first attempt lands nested (incident mode)
                else:
                    self.nested = False
                    self.dst_keys = list(self.src_keys)

            def rm(self, path, recursive=False):
                calls.append(("rm", path, recursive))
                if path.endswith("/modis"):
                    self.nested = False
                    self.dst_keys = []
                elif path.endswith("/_modis_sorted"):
                    self.src_keys = []

        class _FakeStore:
            path = "s3://atlantis/zarr/2025/datacube.zarr"

        monkeypatch.setattr("atlantis.archive.reindex_time.s3fs.S3FileSystem", lambda **kw: _FakeFS())
        _swap_group(_FakeStore(), "modis", "_modis_sorted", {})
        assert [c[0] for c in calls].count("copy") == 2  # nested attempt retried, not doomed
        assert calls[-1] == ("rm", "s3://atlantis/zarr/2025/datacube.zarr/_modis_sorted", True)

    def test_remote_swap_keeps_complete_landing_on_retry(self, tmp_path, monkeypatch):
        """A retry re-verifies the destination first: a complete flat landing is kept, not re-copied."""
        from atlantis.archive.reindex_time import _swap_group

        monkeypatch.setattr("atlantis.archive.reindex_time.time.sleep", lambda _s: None)
        calls = []

        class _FakeFS:
            def __init__(self):
                self.src_keys = ["a", "b", "c"]
                self.dst_keys = []
                self.hidden = True  # attempt 0 verification sees a lagging listing

            def find(self, path):
                calls.append(("find", path))
                if path.endswith("/_modis_sorted"):
                    return list(self.src_keys)
                return list(self.dst_keys)

            def exists(self, path):
                calls.append(("exists", path))
                if not path.endswith("/modis/zarr.json"):
                    return False
                if self.hidden:
                    self.hidden = False  # zarr.json exists but was not listed yet
                    return False
                return bool(self.dst_keys)

            def copy(self, src, dst, recursive=False, on_error=None):
                calls.append(("copy", src, dst))
                self.dst_keys = list(self.src_keys)  # copy lands flat and complete

            def rm(self, path, recursive=False):
                calls.append(("rm", path, recursive))
                if path.endswith("/modis"):
                    self.dst_keys = []
                elif path.endswith("/_modis_sorted"):
                    self.src_keys = []

        class _FakeStore:
            path = "s3://atlantis/zarr/2025/datacube.zarr"

        monkeypatch.setattr("atlantis.archive.reindex_time.s3fs.S3FileSystem", lambda **kw: _FakeFS())
        _swap_group(_FakeStore(), "modis", "_modis_sorted", {})
        assert [c[0] for c in calls].count("copy") == 1  # the good landing is reused, not re-copied
        assert calls[-1] == ("rm", "s3://atlantis/zarr/2025/datacube.zarr/_modis_sorted", True)

    def test_remote_swap_refuses_to_promote_onto_residue(self, tmp_path, monkeypatch):
        """A destination that never reaches zero files aborts the swap before any copy."""
        from atlantis.archive.reindex_time import _swap_group

        monkeypatch.setattr("atlantis.archive.reindex_time.time.sleep", lambda _s: None)

        class _FakeFS:
            def find(self, path):
                if path.endswith("/_modis_sorted"):
                    return ["a", "b", "c"]
                return ["x", "y"]  # never zero

            def rm(self, path, recursive=False):
                pass  # flaky delete: nothing gets removed

            def copy(self, src, dst, recursive=False, on_error=None):
                raise AssertionError("copy must not run onto a non-empty destination")

        class _FakeStore:
            path = "s3://atlantis/zarr/2025/datacube.zarr"

        monkeypatch.setattr("atlantis.archive.reindex_time.s3fs.S3FileSystem", lambda **kw: _FakeFS())
        with pytest.raises(RuntimeError, match="refusing to promote onto residue"):
            _swap_group(_FakeStore(), "modis", "_modis_sorted", {})

    def test_remote_swap_retries_silently_empty_copy(self, tmp_path, monkeypatch):
        """A copy that silently sees an empty listing (lagging store) must retry, not succeed."""
        from atlantis.archive.reindex_time import _swap_group

        monkeypatch.setattr("atlantis.archive.reindex_time.time.sleep", lambda _s: None)

        class _FakeFS:
            def __init__(self):
                self.dst_count = 0

            def find(self, path):
                if path.endswith("/_modis_sorted"):
                    return ["a", "b", "c"]
                return ["x"] * self.dst_count

            def copy(self, src, dst, recursive=False, on_error=None):
                pass  # silently copies nothing — the incident failure mode

            def rm(self, path, recursive=False):
                raise AssertionError("rm called while the copy is incomplete")

        class _FakeStore:
            path = "s3://atlantis/zarr/2025/datacube.zarr"

        monkeypatch.setattr("atlantis.archive.reindex_time.s3fs.S3FileSystem", lambda **kw: _FakeFS())
        with pytest.raises(RuntimeError, match="could not promote"):
            _swap_group(_FakeStore(), "modis", "_modis_sorted", {})

    def test_remote_swap_refuses_empty_source_listing(self, tmp_path, monkeypatch):
        """A zero source count is never trusted — the swap must not delete anything."""
        from atlantis.archive.reindex_time import _swap_group

        monkeypatch.setattr("atlantis.archive.reindex_time.time.sleep", lambda _s: None)

        class _FakeFS:
            def find(self, path):
                return []

            def copy(self, src, dst, recursive=False, on_error=None):
                raise AssertionError("copy must not run without a verified source count")

            def rm(self, path, recursive=False):
                raise AssertionError("rm must not run")

        class _FakeStore:
            path = "s3://atlantis/zarr/2025/datacube.zarr"

        monkeypatch.setattr("atlantis.archive.reindex_time.s3fs.S3FileSystem", lambda **kw: _FakeFS())
        with pytest.raises(RuntimeError, match="refusing to swap"):
            _swap_group(_FakeStore(), "modis", "_modis_sorted", {})

    def test_consolidate_retries_until_group_visible(self, tmp_path, monkeypatch):
        """Consolidation is verified through the consolidated root readers will open."""
        from atlantis.archive.reindex_time import _consolidate_verified

        monkeypatch.setattr("atlantis.archive.reindex_time.time.sleep", lambda _s: None)
        attempts = []

        def fake_consolidate(store):
            attempts.append(1)

        def fake_open_group(store, mode="r"):
            if len(attempts) < 2:
                return {"modis": None}  # stale consolidated root: no group yet
            return {"modis": type("G", (), {"__getitem__": lambda s, k: type("T", (), {"shape": (333,)})()})()}

        monkeypatch.setattr("atlantis.archive.reindex_time.datacube.consolidate", fake_consolidate)
        monkeypatch.setattr("atlantis.archive.reindex_time.zarr.open_group", fake_open_group)
        _consolidate_verified(object(), "modis", 333)
        assert len(attempts) == 2

        def fake_open_group_never(store, mode="r"):
            return {}

        monkeypatch.setattr("atlantis.archive.reindex_time.zarr.open_group", fake_open_group_never)
        with pytest.raises(RuntimeError, match="does not contain"):
            _consolidate_verified(object(), "modis", 333)


class TestLauncher:
    def test_worker_command_round_trip(self, tmp_path):
        opts = _opts(tmp_path, year=2026, start=date(2026, 1, 1), dry_run=True, retry_failed=False)
        args = build_worker_command(opts, "20260802T000000Z")
        assert args[:6] == ["python", "-m", "atlantis.cli", "archive", "modis", "_run-update"]
        assert "--year" in args and "2026" in args
        assert args[-1] == "--dry-run"
        assert "--no-retry-failed" in args

    def test_missing_tmux_fails_clearly(self, tmp_path, monkeypatch):
        opts = _opts(tmp_path, start=date(2026, 1, 1), end=date(2026, 1, 2))
        monkeypatch.setattr("atlantis.archive.update.shutil.which", lambda _c: None)
        with pytest.raises(UpdateError, match="tmux is not installed"):
            launch_tmux_update(opts, run_id="run", repo_root=tmp_path)

    def test_existing_session_fails_clearly(self, tmp_path, monkeypatch):
        opts = _opts(tmp_path, start=date(2026, 1, 1), end=date(2026, 1, 2))
        calls = []
        monkeypatch.setattr("atlantis.archive.update.shutil.which", lambda _c: "/usr/bin/tmux")
        monkeypatch.setattr(
            "atlantis.archive.update.subprocess.run",
            lambda args, **kw: calls.append(args) or type("R", (), {"returncode": 0})(),
        )
        with pytest.raises(UpdateError, match="already exists"):
            launch_tmux_update(opts, run_id="run", repo_root=tmp_path)
        assert calls[0] == ["tmux", "has-session", "-t", "atlantis-modis-update-2026-run"]

    def test_launches_detached_session_with_repo_root(self, tmp_path, monkeypatch):
        opts = _opts(tmp_path, start=date(2026, 1, 1), end=date(2026, 1, 2))
        calls = []
        results = iter([type("R", (), {"returncode": 1})(), type("R", (), {"returncode": 0})()])
        monkeypatch.setattr("atlantis.archive.update.shutil.which", lambda _c: "/usr/bin/tmux")
        monkeypatch.setattr(
            "atlantis.archive.update.subprocess.run", lambda args, **kw: calls.append(args) or next(results)
        )
        name, log_path, command = launch_tmux_update(opts, run_id="run", repo_root=Path("/repo"))
        assert name == "atlantis-modis-update-2026-run"
        assert "cd /repo && PYTHONPATH=src pixi run -e batch" in command
        assert "atlantis.cli archive modis _run-update" in command
        assert command.endswith(f"> {log_path} 2>&1")
        assert str(log_path).endswith(f"{tmp_path}/state/2026/logs/run.log")


class TestSeedTracker:
    TILES = [(10, 3), (11, 3)]

    def _setup(self, tmp_path, monkeypatch, dates):
        from atlantis.archive.update import seed_tracker

        opts = _opts(tmp_path)
        opts.catalogue_builder = FakeCatalogueBuilder(2025, self.TILES, dates=dates)
        monkeypatch.setattr("atlantis.archive.update.harmonise_modis_granule_payload", _payload_for)
        monkeypatch.setattr("atlantis.archive.update.probe_download", lambda url: None)
        fake_run = _fake_run_cube_batch(_payload_for)
        monkeypatch.setattr("atlantis.archive.update.run_cube_batch", fake_run)
        opts.start, opts.end = dates[0], dates[-1]
        _build_legacy_axis(opts, 2025, dates, self.TILES)  # partial axis: prefill is skipped (data guard)
        run_update(opts)
        return opts, seed_tracker

    def test_seeds_done_for_axis_dates_and_reports_pending(self, tmp_path, monkeypatch):
        dates = [date(2025, 1, d) for d in (1, 2, 3, 5)]
        opts, seed_tracker = self._setup(tmp_path, monkeypatch, dates)
        # simulate a lost tracker: archive is intact, tracker is gone
        tracker_path(opts, 2025).unlink()
        summary = seed_tracker(opts, 2025)
        assert summary["seeded"] == 8  # dates 1,2,3,5 on the axis × 2 tiles
        assert summary["pending_dates"] == []  # every catalogue date is on the axis
        done = {tid for tid, st in read_tracker(tracker_path(opts, 2025)).items() if st == "DONE"}
        assert done == set(pd.read_parquet(catalogue_uri(opts, 2025))["task_id"])

    def test_catalogue_date_missing_from_axis_stays_pending(self, tmp_path, monkeypatch):
        # archive covers dates 1-3; catalogue additionally lists date 4 (never written)
        dates = [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3)]
        opts, seed_tracker = self._setup(tmp_path, monkeypatch, dates)
        opts.catalogue_builder.dates = [*dates, date(2025, 1, 4)]
        from atlantis.archive.update import refresh_catalogue

        refresh_catalogue(opts, 2025, date(2025, 1, 4), date(2025, 1, 4), "extra")  # extends the catalogue
        tracker_path(opts, 2025).unlink()
        summary = seed_tracker(opts, 2025)
        assert summary["seeded"] == 6  # dates 1,2,3 on the axis × 2 tiles
        assert summary["pending_dates"] == [date(2025, 1, 4)]
        tracker = read_tracker(tracker_path(opts, 2025))
        assert "modis-20250104-h10v03" not in tracker  # not seeded

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        dates = [date(2025, 1, 1), date(2025, 1, 2)]
        opts, seed_tracker = self._setup(tmp_path, monkeypatch, dates)
        tracker_path(opts, 2025).unlink()
        summary = seed_tracker(opts, 2025, dry_run=True)
        assert summary["seeded"] == 4 and summary["dry_run"] is True
        assert read_tracker(tracker_path(opts, 2025)) == {}

    def test_idempotent(self, tmp_path, monkeypatch):
        dates = [date(2025, 1, 1), date(2025, 1, 2)]
        opts, seed_tracker = self._setup(tmp_path, monkeypatch, dates)
        tracker_path(opts, 2025).unlink()
        seed_tracker(opts, 2025)
        first = read_tracker(tracker_path(opts, 2025))
        seed_tracker(opts, 2025)
        assert read_tracker(tracker_path(opts, 2025)) == first

    def test_missing_archive_group_fails_cleanly(self, tmp_path):
        from atlantis.archive.update import UpdateError, refresh_catalogue, seed_tracker

        opts = _opts(tmp_path)
        opts.catalogue_builder = FakeCatalogueBuilder(2025, self.TILES, dates=[date(2025, 1, 1)])
        refresh_catalogue(opts, 2025, date(2025, 1, 1), date(2025, 1, 1), "r1")  # catalogue exists…
        with pytest.raises(UpdateError, match="no modis group"):
            seed_tracker(opts, 2025)  # …but the archive has no modis group

    def test_refuses_prefilled_year(self, tmp_path):
        """Axis dates are not evidence of data on a prefilled year — refuse."""
        from atlantis.archive.update import UpdateError, refresh_catalogue, seed_tracker
        from atlantis.archive.writer import ArchiveWriter

        opts = _opts(tmp_path)
        opts.catalogue_builder = FakeCatalogueBuilder(2025, self.TILES, dates=[date(2025, 1, 1)])
        refresh_catalogue(opts, 2025, date(2025, 1, 1), date(2025, 1, 1), "r1")
        # build a prefilled year archive (as the cube-build CLI would)
        writer = ArchiveWriter(archive_root(opts, 2025))
        with writer.session("modis", list(MODIS_VAR_NAMES), prefill_year=2025):
            pass
        with pytest.raises(UpdateError, match="prefilled"):
            seed_tracker(opts, 2025)

    def test_cli_seed_tracker(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        import atlantis.cli

        dates = [date(2025, 1, 1), date(2025, 1, 2)]
        opts, _ = self._setup(tmp_path, monkeypatch, dates)
        tracker_path(opts, 2025).unlink()
        result = CliRunner().invoke(
            atlantis.cli.cli,
            [
                "archive",
                "modis",
                "seed-tracker",
                "--year",
                "2025",
                "--state-root",
                str(tmp_path / "state"),
                "--archive-base",
                str(tmp_path / "zarr"),
                "--catalogue-base",
                str(tmp_path / "assets"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Seeded 4 DONE task(s)" in result.output
        assert len(read_tracker(tracker_path(opts, 2025))) == 4


# ── Integration (local store, fake catalogue + fake batch engine) ───────────


class TestIntegration:
    TILES = [(10, 3), (11, 3)]

    def _setup(self, tmp_path, monkeypatch, year=2026, dates=None, tiles=None):
        opts = _opts(tmp_path)
        opts.catalogue_builder = FakeCatalogueBuilder(year, tiles or self.TILES, dates=dates)
        monkeypatch.setattr("atlantis.archive.update.harmonise_modis_granule_payload", _payload_for)
        monkeypatch.setattr("atlantis.archive.update.probe_download", lambda url: None)
        return opts

    def _run_window(self, opts, monkeypatch, start, end, fail_task_ids=()):
        fake_run = _fake_run_cube_batch(_payload_for, fail_task_ids)
        monkeypatch.setattr("atlantis.archive.update.run_cube_batch", fake_run)
        opts.start, opts.end = start, end
        return run_update(opts)

    def test_new_year_updates_land_in_prefilled_slots(self, tmp_path, monkeypatch):
        """A new year's first update pre-fills the full axis; weekly runs land in slots."""
        year = 2026
        opts = self._setup(tmp_path, monkeypatch, year, dates=[date(2026, 1, 1), date(2026, 1, 2)])
        summary = self._run_window(opts, monkeypatch, date(2026, 1, 1), date(2026, 1, 2))
        assert summary["status"] == "ok"
        _, axis = read_archive_dates(opts, year)
        assert len(axis) == 365 and axis[0] == date(2026, 1, 1) and axis[-1] == date(2026, 12, 31)
        assert group_is_prefilled(_modis_group(opts, year))  # marker written on prefill
        assert summary["years"][0]["watermark"] == "2026-01-02"

        # new publication arrives; the weekly append keeps the axis ascending
        opts.catalogue_builder = FakeCatalogueBuilder(
            year, self.TILES, dates=[date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
        )
        summary = self._run_window(opts, monkeypatch, date(2026, 1, 2), date(2026, 1, 3))
        _, axis = read_archive_dates(opts, year)
        assert len(axis) == 365  # prefill is a no-op once the axis is full
        assert summary["years"][0]["watermark"] == "2026-01-03"

        manifests = sorted((tmp_path / "state" / str(year) / "manifests").glob("*.json"))
        assert len(manifests) == 2
        last = json.loads(manifests[-1].read_text())
        assert last["status"] == "ok" and last["watermark"] == "2026-01-03"
        assert last["catalogue_checksum"]
        backup = tmp_path / "backup" / str(year)
        assert (backup / "cube_tracker.db").exists()
        assert (backup / "modis-2026.parquet").exists()
        assert (backup / manifests[-1].name).exists()

        s = stats(tracker_path(opts, year))
        assert s.get("DONE") == 6 and s.get("FAILED", 0) == 0

    def test_hole_requires_reindex_then_fills(self, tmp_path, monkeypatch):
        year = 2026
        dates = [date(2026, 1, d) for d in (1, 2, 3)]
        opts = self._setup(tmp_path, monkeypatch, year, dates=dates)
        _build_legacy_axis(opts, year, [date(2026, 1, 1)], self.TILES)  # legacy partial axis: prefill skipped
        fail_ids = [f"modis-20260102-h{h:02d}v{v:02d}" for h, v in self.TILES]

        # run 1: date 2's tiles fail → FAILED run leaves axis [1, 3]
        with pytest.raises(UpdateError, match="not DONE"):
            self._run_window(opts, monkeypatch, date(2026, 1, 1), date(2026, 1, 3), fail_task_ids=fail_ids)
        _, axis = read_archive_dates(opts, year)
        assert axis == [date(2026, 1, 1), date(2026, 1, 3)]

        # append-only policy: a retry run must refuse to append the earlier hole
        with pytest.raises(UpdateError, match="append-only policy"):
            self._run_window(opts, monkeypatch, date(2026, 1, 1), date(2026, 1, 3))

        # offline migration inserts the missing slot, then the rerun fills it
        from atlantis.archive import _store

        store = _store.store_for(archive_root(opts, year), "datacube.zarr", None)
        df = pd.read_parquet(catalogue_uri(opts, year))
        expected = sorted(set(pd.to_datetime(df["date"]).dt.date))
        reindex_group_time(store, "modis", list(MODIS_VAR_NAMES), expected_dates=expected)
        _, axis = read_archive_dates(opts, year)
        assert axis == [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]

        summary = self._run_window(opts, monkeypatch, date(2026, 1, 1), date(2026, 1, 3))
        assert summary["status"] == "ok"
        assert summary["years"][0]["watermark"] == "2026-01-03"
        assert summary["years"][0]["time_axis_sorted"] is True

    def test_no_retry_failed_leaves_failures_alone_but_stalls_watermark(self, tmp_path, monkeypatch):
        year = 2026
        dates = [date(2026, 1, d) for d in (1, 2, 3)]
        opts = self._setup(tmp_path, monkeypatch, year, dates=dates)
        fail_ids = [f"modis-20260102-h{h:02d}v{v:02d}" for h, v in self.TILES]
        with pytest.raises(UpdateError, match="not DONE"):
            self._run_window(opts, monkeypatch, date(2026, 1, 1), date(2026, 1, 3), fail_task_ids=fail_ids)

        # rerun without retrying FAILED tasks: succeeds, but the watermark stops at date 1
        opts.retry_failed = False
        summary = self._run_window(opts, monkeypatch, date(2026, 1, 1), date(2026, 1, 3))
        assert summary["status"] == "ok"
        assert summary["years"][0]["watermark"] == "2026-01-01"
        s = stats(tracker_path(opts, year))
        assert s.get("FAILED", 0) == 2  # still failed, untouched

    def test_status_report(self, tmp_path, monkeypatch):
        opts = self._setup(tmp_path, monkeypatch, 2026, dates=[date(2026, 1, 1)])
        self._run_window(opts, monkeypatch, date(2026, 1, 1), date(2026, 1, 1))
        report = status_report(opts, 2026)
        assert report["tracker_exists"] is True
        assert report["DONE"] == 2
        assert report["watermark"] == date(2026, 1, 1)
        assert report["time_axis_sorted"] is True
        assert report["catalogue_rows"] == 2
        assert report["last_manifest"]["status"] == "ok"
        assert report["lock"] is None
        assert report["missing_ranges"] == []  # catalogue dates all on the axis
        assert report["date_states"] == {date(2026, 1, 1): "done"}
        assert report["expected_tasks"] == 2 and report["pending_tasks"] == 0
        assert report["state_counts"] == {"done": 1, "failed": 0, "pending": 0, "empty": 364}
        assert report["state_ranges"]["done"] == [(date(2026, 1, 1), date(2026, 1, 1))]

    def test_status_report_prefilled_missing_ranges_from_tracker(self, tmp_path, monkeypatch):
        """On a prefilled year, missing ranges come from the tracker, not the axis."""
        from atlantis.archive.writer import ArchiveWriter

        year = 2026
        dates = [date(2026, 1, 1), date(2026, 1, 2)]
        opts = self._setup(tmp_path, monkeypatch, year, dates=dates)
        refresh_catalogue(opts, year, dates[0], dates[-1], "r1")
        writer = ArchiveWriter(archive_root(opts, year))
        with writer.session("modis", list(MODIS_VAR_NAMES), prefill_year=year):
            pass

        db = tracker_path(opts, year)
        init_db(db)
        df = pd.read_parquet(catalogue_uri(opts, year))
        for tid in df[df["date"] == "2026-01-01"]["task_id"]:
            mark_done(db, tid, "x")

        report = status_report(opts, year)
        assert report["prefilled_year"] is True
        assert report["archive_dates"] == 365  # axis covers the whole year by construction
        # the axis cannot show the missing date 1/2 — only the tracker can
        assert report["missing_ranges"] == [(date(2026, 1, 2), date(2026, 1, 2))]
        assert report["date_states"] == {date(2026, 1, 1): "done", date(2026, 1, 2): "pending"}

    def test_status_report_non_prefilled_missing_from_axis(self, tmp_path, monkeypatch):
        """Legacy non-prefilled years keep the axis-based missing-range semantics."""
        year = 2026
        opts = self._setup(tmp_path, monkeypatch, year, dates=[date(2026, 1, 1)])
        _build_legacy_axis(opts, year, [date(2026, 1, 1)])  # legacy partial axis: prefill skipped
        self._run_window(opts, monkeypatch, date(2026, 1, 1), date(2026, 1, 1))
        # catalogue lists date 2 (never written); axis still only holds date 1
        opts.catalogue_builder.dates = [date(2026, 1, 1), date(2026, 1, 2)]
        refresh_catalogue(opts, year, date(2026, 1, 2), date(2026, 1, 2), "extra")
        report = status_report(opts, year)
        assert report["prefilled_year"] is False
        assert report["missing_ranges"] == [(date(2026, 1, 2), date(2026, 1, 2))]

    def test_rollover_auto_prepares_next_year(self, tmp_path, monkeypatch):
        """A backlog spanning the year boundary builds, prefills, and fills the new year."""
        opts = _opts(tmp_path)
        opts.today = date(2027, 1, 12)
        opts.catalogue_builder = _any_year_builder
        monkeypatch.setattr("atlantis.archive.update.harmonise_modis_granule_payload", _payload_for)
        fake_run = _fake_run_cube_batch(_payload_for)
        monkeypatch.setattr("atlantis.archive.update.run_cube_batch", fake_run)

        summary = run_update(opts)
        assert summary["status"] == "ok"
        assert [y["year"] for y in summary["years"]] == [2026, 2027]
        assert summary["years"][0]["watermark"] == "2026-12-31"
        assert summary["years"][1]["watermark"] == "2027-01-05"

        _, axis = read_archive_dates(opts, 2027)
        assert len(axis) == 365 and axis[-1] == date(2027, 12, 31)
        assert group_is_prefilled(_modis_group(opts, 2027))  # prepared: catalogue + prefill + fill
        report = status_report(opts, 2027)
        assert report["prefilled_year"] is True

    def test_dry_run_makes_no_writes(self, tmp_path, monkeypatch):
        opts = self._setup(tmp_path, monkeypatch, 2026, dates=[date(2026, 1, 1)])
        opts.dry_run = True
        summary = run_update(opts)
        assert summary["years"][0]["dry_run"] is True
        assert not tracker_path(opts, 2026).exists()
        assert read_archive_dates(opts, 2026) == (set(), [])

    def test_failed_run_manifests_and_backs_up(self, tmp_path, monkeypatch):
        opts = self._setup(tmp_path, monkeypatch, 2026, dates=[date(2026, 1, 1)])
        fail_ids = [f"modis-20260101-h{h:02d}v{v:02d}" for h, v in self.TILES]
        with pytest.raises(UpdateError):
            self._run_window(opts, monkeypatch, date(2026, 1, 1), date(2026, 1, 1), fail_task_ids=fail_ids)
        manifests = sorted((tmp_path / "state" / "2026" / "manifests").glob("*.json"))
        assert manifests and json.loads(manifests[-1].read_text())["status"] == "failed"
        assert (tmp_path / "backup" / "2026" / "cube_tracker.db").exists()
