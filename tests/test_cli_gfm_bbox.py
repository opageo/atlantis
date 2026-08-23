"""Tests for ``atlantis batch gfm cube run`` mode branching.

Covers the two task sources: catalog mode (``--inventory``) and bbox mode
(``--bbox`` + ``--start-date`` + ``--end-date``), plus the mode-selection
validation.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
from typer.testing import CliRunner

from atlantis.cli import cli

runner = CliRunner()


def _task(event_id: str, day: str) -> dict:
    return {
        "task_id": f"gfm-{event_id}-1-EU020M_E036N009T3-{day.replace('-', '')}",
        "date": day,
        "equi7_tile": "EU020M_E036N009T3",
        "item_hrefs": ["s3://x/item.json"],
        "bbox": [1.0, 2.0, 3.0, 4.0],
        "event_id": event_id,
        "aoi_id": "1",
    }


def _catalogue(tmp_path: Path, dates: list[str]) -> Path:
    rows = [
        {
            "date": d,
            "equi7_tile": "EU020M_E036N009T3",
            "item_id": f"i{i}",
            "item_href": f"s3://x/{i}.json",
            "west": 1.0,
            "south": 2.0,
            "east": 3.0,
            "north": 4.0,
        }
        for i, d in enumerate(dates)
    ]
    path = tmp_path / "catalogue.parquet"
    pd.DataFrame(rows).to_parquet(path)
    return path


class TestCubeRunModes:
    """Mode selection + shared batch wiring for `batch gfm cube run`."""

    def _patch_deps(self, monkeypatch, tasks=None, dropped=None):
        captured: dict = {}

        def fake_build(event_id, bbox, start, end, buffer_km):
            captured["build"] = (event_id, bbox, start, end, buffer_km)
            return tasks if tasks is not None else [_task(event_id, start.isoformat())], dropped or []

        monkeypatch.setattr("atlantis.fetchers.gfm.event_tasks.build_tasks_for_window", fake_build)

        def fake_run(task_list, **kw):
            captured["run"] = (task_list, kw)
            return {"DONE": len(task_list), "FAILED": 0}

        monkeypatch.setattr("atlantis.archive.cube_batch.run_gfm_cube_batch", fake_run)
        monkeypatch.setattr("atlantis.cli._host_ram_bytes", lambda: 64 << 30)
        monkeypatch.setattr("atlantis.cli._validate_prefilled_axis", lambda *a: captured.setdefault("validated", a))
        return captured

    def _invoke(self, tmp_path: Path, extra_args: tuple[str, ...] = ()):
        return runner.invoke(
            cli,
            [
                "batch",
                "gfm",
                "cube",
                "run",
                "--event",
                "custom",
                "--bbox",
                "1 2 3 4",
                "--start-date",
                "2020-08-01",
                "--end-date",
                "2020-08-02",
                "--archive",
                str(tmp_path / "zarr" / "2020"),
                "--db-path",
                str(tmp_path / "tracker.db"),
                *extra_args,
            ],
        )

    # ── bbox mode ────────────────────────────────────────────────────────────

    def test_bbox_mode_tasks_only_writes_task_list(self, tmp_path, monkeypatch):
        captured = self._patch_deps(
            monkeypatch,
            tasks=[_task("custom", "2020-08-01")],
            dropped=[{"item_id": "x", "item_href": "s3://x", "reason": "missing bbox"}],
        )
        tasks_file = tmp_path / "tasks.json"
        result = self._invoke(tmp_path, ("--tasks-only", "--tasks", str(tasks_file)))

        assert result.exit_code == 0, result.output
        assert json.loads(tasks_file.read_text())[0]["task_id"].startswith("gfm-custom-")
        assert tasks_file.with_name("tasks.dropped.json").exists()
        assert "Task list written" in result.output
        assert "run" not in captured  # the batch must not start under --tasks-only

    def test_bbox_mode_full_run_resolves_prefill_and_validates(self, tmp_path, monkeypatch):
        captured = self._patch_deps(monkeypatch, tasks=[_task("custom", "2020-08-01")])
        archive = tmp_path / "zarr" / "2020"
        result = self._invoke(tmp_path)

        assert result.exit_code == 0, result.output
        assert captured["run"][1]["archive_root"] == str(archive)
        assert captured["run"][1]["prefill_year"] == 2020  # auto-detected from the zarr/YYYY root
        assert captured["run"][1]["storage_options"] is None
        assert captured["validated"][:4] == (str(archive), "gfm", captured["run"][0], 2020)

    def test_bbox_mode_forwarded_build_args_and_no_prefill(self, tmp_path, monkeypatch):
        captured = self._patch_deps(monkeypatch, tasks=[])
        result = self._invoke(tmp_path, ("--buffer-km", "0", "--no-prefill"))

        assert result.exit_code == 0, result.output
        assert captured["build"] == ("custom", (1.0, 2.0, 3.0, 4.0), date(2020, 8, 1), date(2020, 8, 2), 0.0)
        assert captured["run"][1]["prefill_year"] is None
        assert "validated" not in captured  # no prefill → no post-run axis validation

    # ── catalog mode ─────────────────────────────────────────────────────────

    def test_catalog_mode_uses_inventory(self, tmp_path, monkeypatch):
        captured = self._patch_deps(monkeypatch)
        catalogue = _catalogue(tmp_path, ["2024-10-29", "2024-11-01"])
        result = runner.invoke(
            cli,
            [
                "batch",
                "gfm",
                "cube",
                "run",
                "--inventory",
                str(catalogue),
                "--archive",
                str(tmp_path / "zarr" / "2024"),
                "--db-path",
                str(tmp_path / "tracker.db"),
            ],
        )

        assert result.exit_code == 0, result.output
        assert len(captured["run"][0]) == 2  # one cell per (date, tile)
        assert "cells" in result.output
        assert captured["run"][1]["prefill_year"] == 2024
        assert "build" not in captured  # bbox task builder must not run in catalog mode

    def test_catalog_mode_tasks_only(self, tmp_path, monkeypatch):
        captured = self._patch_deps(monkeypatch)
        catalogue = _catalogue(tmp_path, ["2024-10-29"])
        tasks_file = tmp_path / "tasks.json"
        result = runner.invoke(
            cli,
            [
                "batch",
                "gfm",
                "cube",
                "run",
                "--inventory",
                str(catalogue),
                "--archive",
                str(tmp_path / "zarr" / "2024"),
                "--db-path",
                str(tmp_path / "tracker.db"),
                "--tasks-only",
                "--tasks",
                str(tasks_file),
            ],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(tasks_file.read_text())[0]["task_id"] == "gfm-20241029-EU020M_E036N009T3"
        assert "run" not in captured

    # ── mode-selection validation ────────────────────────────────────────────

    def test_inventory_and_bbox_conflict_fails(self, tmp_path, monkeypatch):
        self._patch_deps(monkeypatch, tasks=[])
        catalogue = _catalogue(tmp_path, ["2024-10-29"])
        result = runner.invoke(
            cli,
            [
                "batch",
                "gfm",
                "cube",
                "run",
                "--inventory",
                str(catalogue),
                "--bbox",
                "1 2 3 4",
                "--start-date",
                "2024-10-29",
                "--end-date",
                "2024-11-01",
            ],
        )
        assert result.exit_code != 0
        assert "cannot be combined" in result.output

    def test_no_task_source_fails(self, tmp_path, monkeypatch):
        self._patch_deps(monkeypatch, tasks=[])
        result = runner.invoke(
            cli,
            [
                "batch",
                "gfm",
                "cube",
                "run",
                "--archive",
                str(tmp_path / "zarr" / "2020"),
                "--db-path",
                str(tmp_path / "tracker.db"),
            ],
        )
        assert result.exit_code != 0
        assert "No task source" in result.output

    def test_partial_bbox_trio_fails(self, tmp_path, monkeypatch):
        self._patch_deps(monkeypatch, tasks=[])
        result = runner.invoke(
            cli,
            [
                "batch",
                "gfm",
                "cube",
                "run",
                "--bbox",
                "1 2 3 4",
                "--start-date",
                "2020-08-01",
            ],
        )
        assert result.exit_code != 0
        assert "provided together" in result.output

    def test_partition_in_bbox_mode_fails(self, tmp_path, monkeypatch):
        self._patch_deps(monkeypatch, tasks=[])
        result = self._invoke(tmp_path, ("--partition", "0:10"))
        assert result.exit_code != 0
        assert "only valid in catalog mode" in result.output

    def test_invalid_bbox_fails(self, tmp_path, monkeypatch):
        self._patch_deps(monkeypatch, tasks=[])
        result = runner.invoke(
            cli,
            [
                "batch",
                "gfm",
                "cube",
                "run",
                "--event",
                "custom",
                "--bbox",
                "1 2 3",
                "--start-date",
                "2020-08-01",
                "--end-date",
                "2020-08-02",
            ],
        )
        assert result.exit_code != 0
        assert "exactly four numbers" in result.output

    def test_bbox_out_of_range_fails(self, tmp_path, monkeypatch):
        self._patch_deps(monkeypatch, tasks=[])
        result = runner.invoke(
            cli,
            [
                "batch",
                "gfm",
                "cube",
                "run",
                "--event",
                "custom",
                "--bbox",
                "200 0 201 1",
                "--start-date",
                "2020-08-01",
                "--end-date",
                "2020-08-02",
            ],
        )
        assert result.exit_code != 0
        assert "valid west south east north bbox" in result.output

    def test_invalid_date_fails(self, tmp_path, monkeypatch):
        self._patch_deps(monkeypatch, tasks=[])
        result = runner.invoke(
            cli,
            [
                "batch",
                "gfm",
                "cube",
                "run",
                "--event",
                "custom",
                "--bbox",
                "1 2 3 4",
                "--start-date",
                "2020-08-01",
                "--end-date",
                "not-a-date",
            ],
        )
        assert result.exit_code != 0
        assert "YYYY-MM-DD" in result.output
