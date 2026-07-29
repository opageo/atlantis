"""Tests for harmonised GFM artifact validation."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from typer.testing import CliRunner

from atlantis.cli import cli
from atlantis.validation.gfm import (
    CLASSIFIED_LAYER_SUFFIXES,
    GFM_NODATA,
    GFM_RESOLUTION,
    RAW_LAYER_SUFFIXES,
    GfmValidationReport,
    ValidationCheck,
    compare_rasters,
    validate_gfm_artifacts,
)

EVENT_ID = "Valencia_2024"
DATE_TOKEN = "2024-11-01"
TRANSFORM = from_origin(-1.5, 40.0, GFM_RESOLUTION, GFM_RESOLUTION)


def _path(directory: Path, suffix: str) -> Path:
    return directory / f"{EVENT_ID}_{DATE_TOKEN}_{suffix}.tif"


def _write_raster(path: Path, data: np.ndarray, *, transform=TRANSFORM) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
        nodata=GFM_NODATA,
        compress="LZW",
    ) as dst:
        dst.write(data, 1)


def _write_artifacts(directory: Path, *, include_raw: bool = True) -> None:
    directory.mkdir()
    layers = {
        "flood_fraction": np.array([[0, 25], [GFM_NODATA, 100]], dtype=np.uint8),
        "water_fraction": np.array([[0, 50], [GFM_NODATA, 100]], dtype=np.uint8),
        "reference_water": np.array([[0, 1], [GFM_NODATA, 2]], dtype=np.uint8),
        "exclusion_mask": np.array([[0, 4], [GFM_NODATA, 8]], dtype=np.uint8),
        "ensemble_likelihood": np.array([[0, 40], [GFM_NODATA, 100]], dtype=np.uint8),
        "advisory_flags": np.array([[0, 3], [GFM_NODATA, 16]], dtype=np.uint8),
        "ensemble_flood_extent": np.array([[0, 1], [GFM_NODATA, 1]], dtype=np.uint8),
        "reference_water_mask": np.array([[0, 1], [GFM_NODATA, 2]], dtype=np.uint8),
    }
    suffixes = {**CLASSIFIED_LAYER_SUFFIXES, **RAW_LAYER_SUFFIXES}
    for name, suffix in suffixes.items():
        if name in RAW_LAYER_SUFFIXES and not include_raw:
            continue
        _write_raster(_path(directory, suffix), layers[name])


def _copy_classified_artifacts(source: Path, destination: Path) -> None:
    destination.mkdir()
    for suffix in CLASSIFIED_LAYER_SUFFIXES.values():
        destination.joinpath(_path(source, suffix).name).write_bytes(_path(source, suffix).read_bytes())


def _check(report: GfmValidationReport, name: str) -> ValidationCheck:
    return next(check for check in report.checks if check.name == name)


def test_valid_artifacts_pass_with_raw_diagnostics(tmp_path):
    artifacts = tmp_path / "artifacts"
    references = tmp_path / "references"
    _write_artifacts(artifacts)
    _copy_classified_artifacts(artifacts, references)

    report = validate_gfm_artifacts(artifacts, reference_base=str(references), require_raw=True)

    assert report.passed
    diagnostic = _check(report, "raw_classified_diagnostics")
    assert diagnostic.required is False
    assert diagnostic.details["joint_flood_pixels"] == 3
    assert _check(report, "reference_flood_fraction").passed


def test_missing_classified_layer_fails(tmp_path):
    artifacts = tmp_path / "artifacts"
    _write_artifacts(artifacts)
    _path(artifacts, CLASSIFIED_LAYER_SUFFIXES["water_fraction"]).unlink()

    report = validate_gfm_artifacts(artifacts, reference_base=str(tmp_path / "references"))

    assert not report.passed
    assert "water_fraction" in _check(report, "classified_artifacts").details["missing"]


def test_mismatched_grid_fails(tmp_path):
    artifacts = tmp_path / "artifacts"
    references = tmp_path / "references"
    _write_artifacts(artifacts)
    _copy_classified_artifacts(artifacts, references)
    _write_raster(
        _path(artifacts, CLASSIFIED_LAYER_SUFFIXES["water_fraction"]),
        np.array([[0, 50], [GFM_NODATA, 100]], dtype=np.uint8),
        transform=from_origin(-1.0, 40.0, GFM_RESOLUTION, GFM_RESOLUTION),
    )

    report = validate_gfm_artifacts(artifacts, reference_base=str(references))

    assert not _check(report, "shared_grid").passed


def test_invalid_layer_domain_fails(tmp_path):
    artifacts = tmp_path / "artifacts"
    references = tmp_path / "references"
    _write_artifacts(artifacts)
    _copy_classified_artifacts(artifacts, references)
    _write_raster(
        _path(artifacts, CLASSIFIED_LAYER_SUFFIXES["water_fraction"]),
        np.array([[0, 101], [GFM_NODATA, 100]], dtype=np.uint8),
    )

    report = validate_gfm_artifacts(artifacts, reference_base=str(references))

    assert not _check(report, "domain_water_fraction").passed


def test_partial_raw_pair_fails_even_when_not_required(tmp_path):
    artifacts = tmp_path / "artifacts"
    references = tmp_path / "references"
    _write_artifacts(artifacts)
    _copy_classified_artifacts(artifacts, references)
    _path(artifacts, RAW_LAYER_SUFFIXES["reference_water_mask"]).unlink()

    report = validate_gfm_artifacts(artifacts, reference_base=str(references))

    assert not _check(report, "raw_artifacts").passed


def test_compare_rasters_strict_mode_requires_identical_bytes(tmp_path):
    produced = tmp_path / "produced.tif"
    reference = tmp_path / "reference.tif"
    data = np.array([[0, 1], [2, 3]], dtype=np.uint8)
    _write_raster(produced, data)
    reference.write_bytes(produced.read_bytes())

    metrics = compare_rasters(produced, reference, require_byte_identity=True)

    assert metrics.mismatch_ratio == 0.0
    _write_raster(reference, np.array([[0, 1], [2, 4]], dtype=np.uint8))
    with pytest.raises(AssertionError, match="Overlap mismatch"):
        compare_rasters(produced, reference)


def test_validate_gfm_cli_returns_nonzero_for_required_failure(monkeypatch):
    report = GfmValidationReport(
        checks=[ValidationCheck(name="classified_artifacts", passed=False, message="Missing classified artifacts")]
    )
    monkeypatch.setattr("atlantis.cli.validate_gfm_artifacts", lambda *_args, **_kwargs: report)

    result = CliRunner().invoke(cli, ["validate-gfm", "--input-dir", "ignored"])

    assert result.exit_code == 1
    assert "GFM validation failed" in result.stdout
