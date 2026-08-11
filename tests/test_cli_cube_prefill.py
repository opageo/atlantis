"""Tests for the cube-run prefill flags, year detection, and axis validation."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import typer
from typer.testing import CliRunner

from atlantis.cli import (
    _check_task_dates_in_year,
    _detect_prefill_year,
    _resolve_prefill_year,
    _validate_prefilled_axis,
    cli,
)

runner = CliRunner()


def _aligned_dataset():
    """A harmonised-style float dataset aligned to the canonical global grid."""
    import numpy as np
    import xarray as xr

    from atlantis.archive import grid

    row0, col0, h, w = 4000, 10000, 50, 60
    y = grid.global_y_coords()[row0 : row0 + h]
    x = grid.global_x_coords()[col0 : col0 + w]
    data = np.full((h, w), 0.5, dtype="float32")
    return xr.Dataset(
        {"water_fraction": xr.DataArray(data, dims=["y", "x"], coords={"y": y, "x": x})},
        attrs={"crs": "EPSG:4326"},
    )


def _viirs_catalogue(tmp_path, dates: list[str]) -> pd.DataFrame:
    rows = [
        {
            "date": d,
            "aoi_id": i,
            "s3_key": f"viirs/2024/{d}/GLB{i:03d}.tif",
            "geometry": None,
        }
        for i, d in enumerate(dates)
    ]
    return pd.DataFrame(rows)


# ── Year detection ────────────────────────────────────────────────────────────


class TestDetectPrefillYear:
    def test_year_roots(self):
        assert _detect_prefill_year("s3://atlantis/zarr/2025") == 2025
        assert _detect_prefill_year("s3://atlantis/zarr/2025/") == 2025
        assert _detect_prefill_year("/data/zarr/2020") == 2020
        assert _detect_prefill_year("zarr/2024") == 2024

    def test_non_year_roots(self):
        assert _detect_prefill_year("s3://atlantis/zarr/viirs_2020_cube") is None
        assert _detect_prefill_year("s3://atlantis/zarr/modis_cube") is None
        assert _detect_prefill_year("s3://atlantis/zarr/gfm_cube") is None
        assert _detect_prefill_year("my_cube") is None
        assert _detect_prefill_year("s3://atlantis/zarr/2025x") is None


# ── Flag resolution ───────────────────────────────────────────────────────────


class TestResolvePrefillYear:
    def test_explicit_overrides_detection(self):
        assert _resolve_prefill_year("s3://atlantis/zarr/2025", 2020, False) == 2020

    def test_auto_detects_year_root(self):
        assert _resolve_prefill_year("s3://atlantis/zarr/2025", None, False) == 2025
        assert _resolve_prefill_year("/data/zarr/2020", None, False) == 2020

    def test_no_prefill_disables_detection(self):
        assert _resolve_prefill_year("s3://atlantis/zarr/2025", None, True) is None

    def test_non_year_root_stays_legacy(self):
        assert _resolve_prefill_year("s3://atlantis/zarr/viirs_2020_cube", None, False) is None

    def test_both_flags_fail(self):
        with pytest.raises(typer.Exit) as exc:
            _resolve_prefill_year("s3://atlantis/zarr/2025", 2025, True)
        assert exc.value.exit_code == 1


# ── Pre-run date-range check ─────────────────────────────────────────────────


class TestCheckTaskDatesInYear:
    def test_accepts_dates_within_year(self):
        _check_task_dates_in_year([{"date": "2020-08-10"}, {"date": "2020-12-31"}], 2020)

    def test_fails_on_out_of_year_date(self):
        with pytest.raises(typer.Exit) as exc:
            _check_task_dates_in_year([{"date": "2020-12-31"}, {"date": "2021-01-01"}], 2020)
        assert exc.value.exit_code == 1


# ── Post-run axis validation ─────────────────────────────────────────────────


class TestValidatePrefilledAxis:
    def _prefilled_cube(self, tmp_path, *, year=2024, dates=("2024-10-29",)):
        from atlantis.archive.writer import ArchiveWriter

        root = tmp_path / "zarr" / str(year)
        writer = ArchiveWriter(root)
        with writer.session("viirs", ["water_fraction"], prefill_year=year) as session:
            for d in dates:
                session.write(_aligned_dataset(), time=date.fromisoformat(d))
        return str(root)

    def test_valid_prefilled_axis_passes(self, tmp_path):
        root = self._prefilled_cube(tmp_path)
        _validate_prefilled_axis(root, "viirs", [{"date": "2024-10-29"}], 2024, None)

    def test_missing_task_date_fails(self, tmp_path):
        root = self._prefilled_cube(tmp_path)
        with pytest.raises(typer.Exit) as exc:
            _validate_prefilled_axis(root, "viirs", [{"date": "2023-01-01"}], 2024, None)
        assert exc.value.exit_code == 1

    def test_unsorted_axis_fails(self, tmp_path):
        root = self._prefilled_cube(tmp_path)
        # corrupt a prefilled axis: swap the first two day ints (marker stays)
        import numpy as np
        import zarr

        group = zarr.open_group(Path(root) / "datacube.zarr", mode="a")["viirs"]
        times = np.asarray(group["time"][:], dtype="int64").copy()
        times[:2] = times[1::-1]
        group["time"][:] = times
        with pytest.raises(typer.Exit) as exc:
            _validate_prefilled_axis(root, "viirs", [{"date": "2024-10-29"}], 2024, None)
        assert exc.value.exit_code == 1

    def test_skips_validation_when_prefill_did_not_run(self, tmp_path):
        """A legacy group (no marker) is never failed by the post-run check."""
        from atlantis.archive.writer import ArchiveWriter

        root = tmp_path / "zarr" / "2024"
        writer = ArchiveWriter(root)
        with writer.session("viirs", ["water_fraction"]) as session:
            session.write(_aligned_dataset(), time=date(2024, 3, 1))
            session.write(_aligned_dataset(), time=date(2024, 1, 15))
        # unsorted and missing a task date — but no marker, so the check skips
        _validate_prefilled_axis(str(root), "viirs", [{"date": "2024-03-01"}, {"date": "2024-01-15"}], 2024, None)


# ── Forwarding into the cube batch runners ───────────────────────────────────


class TestCubeBatchPrefillForwarding:
    def test_viirs_and_modis_forward_prefill_year(self, tmp_path, monkeypatch):
        from atlantis.archive.cube_batch import run_modis_cube_batch, run_viirs_cube_batch
        from atlantis.batch.config import BatchConfig

        calls: list[tuple[str, int | None]] = []

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class FakeWriter:
            def __init__(self, *args, **kwargs):
                pass

            def session(self, source_id, var_names, prefill_year=None):
                calls.append((source_id, prefill_year))
                return FakeSession()

        monkeypatch.setattr("atlantis.archive.writer.ArchiveWriter", FakeWriter)
        monkeypatch.setattr("atlantis.archive.cube_batch.run_cube_batch", lambda *a, **kw: {"DONE": 0, "FAILED": 0})

        cfg = BatchConfig(db_path=tmp_path / "t.db")
        run_viirs_cube_batch([], archive_root="x", cfg=cfg, prefill_year=2024)
        run_modis_cube_batch([], archive_root="x", cfg=cfg, prefill_year=2024)
        run_viirs_cube_batch([], archive_root="x", cfg=cfg)
        assert calls == [("viirs", 2024), ("modis", 2024), ("viirs", None)]


# ── Command-level behaviour ──────────────────────────────────────────────────


class TestCubeRunCommand:
    def _invoke_viirs(self, tmp_path, monkeypatch, catalogue, archive, extra_args=(), db_name="cube_tracker.db"):
        captured: dict = {}

        def fake_run(tasks, **kw):
            captured["tasks"] = tasks
            captured["prefill_year"] = kw.get("prefill_year")
            captured["archive"] = kw["archive_root"]
            return {"DONE": len(tasks), "FAILED": 0}

        monkeypatch.setattr("atlantis.archive.cube_batch.run_viirs_cube_batch", fake_run)
        validate_calls = []
        monkeypatch.setattr("atlantis.cli._validate_prefilled_axis", lambda *a: validate_calls.append(a))
        result = runner.invoke(
            cli,
            [
                "batch",
                "viirs",
                "cube",
                "run",
                "--archive",
                str(archive),
                "--inventory",
                str(catalogue),
                "--db-path",
                str(tmp_path / db_name),
                *extra_args,
            ],
        )
        return result, captured, validate_calls

    def test_detects_year_root_and_runs_with_prefill(self, tmp_path, monkeypatch):
        catalogue = tmp_path / "cat.parquet"
        _viirs_catalogue(tmp_path, ["2024-01-05", "2024-10-29"]).to_parquet(catalogue)
        result, captured, validate_calls = self._invoke_viirs(
            tmp_path, monkeypatch, catalogue, tmp_path / "zarr" / "2024"
        )
        assert result.exit_code == 0, result.output
        assert captured["prefill_year"] == 2024
        assert "day(s) of 2024" in result.output
        # post-run validation ran against the viirs group with the resolved year
        assert validate_calls[0][:4] == (str(tmp_path / "zarr" / "2024"), "viirs", captured["tasks"], 2024)

    def test_explicit_prefill_year_overrides_non_year_root(self, tmp_path, monkeypatch):
        catalogue = tmp_path / "cat.parquet"
        _viirs_catalogue(tmp_path, ["2020-01-01"]).to_parquet(catalogue)
        result, captured, _ = self._invoke_viirs(
            tmp_path,
            monkeypatch,
            catalogue,
            tmp_path / "my_cube",
            extra_args=["--prefill-year", "2020"],
        )
        assert result.exit_code == 0, result.output
        assert captured["prefill_year"] == 2020

    def test_no_prefill_disables_detection(self, tmp_path, monkeypatch):
        catalogue = tmp_path / "cat.parquet"
        _viirs_catalogue(tmp_path, ["2024-01-05"]).to_parquet(catalogue)
        result, captured, validate_calls = self._invoke_viirs(
            tmp_path, monkeypatch, catalogue, tmp_path / "zarr" / "2024", extra_args=["--no-prefill"]
        )
        assert result.exit_code == 0, result.output
        assert captured["prefill_year"] is None
        assert validate_calls == []  # no prefill → no post-run axis validation

    def test_both_flags_fail(self, tmp_path, monkeypatch):
        catalogue = tmp_path / "cat.parquet"
        _viirs_catalogue(tmp_path, ["2024-01-05"]).to_parquet(catalogue)
        result, _, _ = self._invoke_viirs(
            tmp_path,
            monkeypatch,
            catalogue,
            tmp_path / "zarr" / "2024",
            extra_args=["--prefill-year", "2024", "--no-prefill"],
        )
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_out_of_year_dates_fail_before_run(self, tmp_path, monkeypatch):
        catalogue = tmp_path / "cat.parquet"
        _viirs_catalogue(tmp_path, ["2024-12-31", "2025-01-01"]).to_parquet(catalogue)

        def fake_run(tasks, **kw):  # pragma: no cover - must never be reached
            raise AssertionError("run must not start when task dates are out of year")

        monkeypatch.setattr("atlantis.archive.cube_batch.run_viirs_cube_batch", fake_run)
        result = runner.invoke(
            cli,
            [
                "batch",
                "viirs",
                "cube",
                "run",
                "--archive",
                str(tmp_path / "zarr" / "2024"),
                "--inventory",
                str(catalogue),
                "--db-path",
                str(tmp_path / "cube_tracker.db"),
            ],
        )
        assert result.exit_code == 1
        assert "outside prefill year 2024" in result.output
