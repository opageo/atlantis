"""Validation helpers for harmonised Global Flood Monitor artifacts."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import numpy as np
import rasterio
from rasterio.session import AWSSession
from rasterio.windows import from_bounds

GFM_NODATA = 255
GFM_RESOLUTION = 1.0 / 60.0
DEFAULT_REFERENCE_BASE = "s3://atlantis/reference/Valencia_2024_gfm/strategy_all/harmonised"

CLASSIFIED_LAYER_SUFFIXES = {
    "flood_fraction": "gfm_harmonised",
    "water_fraction": "gfm_water_fraction_harmonised",
    "reference_water": "gfm_reference_water_harmonised",
    "exclusion_mask": "gfm_exclusion_mask_harmonised",
    "ensemble_likelihood": "gfm_ensemble_likelihood_harmonised",
    "advisory_flags": "gfm_advisory_flags_harmonised",
}
RAW_LAYER_SUFFIXES = {
    "ensemble_flood_extent": "gfm_ensemble_flood_extent_harmonised",
    "reference_water_mask": "gfm_reference_water_mask_harmonised",
}

MAX_E2E_MISMATCH_RATIO = 0.03
MAX_E2E_MEAN_ABS_DIFF = 0.5
MIN_E2E_ACTIVE_RECALL = 0.30
MIN_E2E_OVERLAP_RATIO = 0.95
HASH_CHUNK_SIZE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ValidationCheck:
    """One mandatory validation result or informational diagnostic."""

    name: str
    passed: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    required: bool = True


@dataclass(frozen=True)
class GfmValidationReport:
    """Full result of validating one date's harmonised GFM artifacts."""

    checks: list[ValidationCheck]

    @property
    def passed(self) -> bool:
        """Whether every required check succeeded."""
        return all(check.passed for check in self.checks if check.required)

    @property
    def failures(self) -> list[ValidationCheck]:
        """Return failed mandatory checks."""
        return [check for check in self.checks if check.required and not check.passed]


@dataclass(frozen=True)
class RasterComparisonMetrics:
    """Metrics measured on an aligned overlapping raster window."""

    overlap_ratio: float
    mismatch_ratio: float
    mean_abs_diff: float
    active_recall: float
    overlap_shape: tuple[int, int, int]


def _path_string(path: str | Path | Any) -> str:
    """Return a rasterio-compatible path string without mangling S3 URIs."""
    try:
        uri = path.as_uri()
    except (AttributeError, ValueError):
        uri = str(path)
    return str(path) if uri.startswith("file://") else uri


def _is_s3_path(path: str | Path | Any) -> bool:
    return _path_string(path).startswith("s3://")


def _sha256_and_size(path: str | Path | Any) -> tuple[str, int]:
    """Calculate a SHA-256 digest and byte size for local or S3 artifacts."""
    digest = hashlib.sha256()
    total_bytes = 0
    location = _path_string(path)
    parsed = urlparse(location)

    if parsed.scheme == "s3":
        import boto3

        response = (
            boto3.Session(profile_name="default")
            .client("s3")
            .get_object(
                Bucket=parsed.netloc,
                Key=parsed.path.lstrip("/"),
            )
        )
        body = response["Body"]
        try:
            while chunk := body.read(HASH_CHUNK_SIZE_BYTES):
                total_bytes += len(chunk)
                digest.update(chunk)
        finally:
            body.close()
    else:
        with Path(location).open("rb") as handle:
            while chunk := handle.read(HASH_CHUNK_SIZE_BYTES):
                total_bytes += len(chunk)
                digest.update(chunk)

    return digest.hexdigest(), total_bytes


@contextmanager
def s3_rasterio_env() -> Iterator[None]:
    """Configure rasterio/GDAL to read the project's S3 reference bucket."""
    import boto3

    session = boto3.Session(profile_name="default")
    endpoint = session.client("s3").meta.endpoint_url.removeprefix("https://")
    with rasterio.Env(
        AWSSession(session),
        AWS_S3_ENDPOINT=endpoint,
        AWS_HTTPS="YES",
        AWS_VIRTUAL_HOSTING="FALSE",
    ):
        yield


def compare_rasters(
    produced: str | Path | Any,
    reference: str | Path | Any,
    *,
    require_byte_identity: bool = False,
) -> RasterComparisonMetrics:
    """Compare two GeoTIFFs on their aligned shared grid.

    This is intentionally tolerant of documented live-source drift and one-cell
    grid-boundary changes. Set ``require_byte_identity`` for a release gate.
    """
    produced_path = _path_string(produced)
    reference_path = _path_string(reference)
    environment = s3_rasterio_env() if _is_s3_path(produced) or _is_s3_path(reference) else nullcontext()

    with environment:
        with rasterio.open(produced_path) as src_p, rasterio.open(reference_path) as src_r:
            assert src_p.crs == src_r.crs, f"CRS mismatch: {src_p.crs} vs {src_r.crs}"
            assert src_p.count == src_r.count, f"Band-count mismatch: {src_p.count} vs {src_r.count}"
            assert src_p.dtypes == src_r.dtypes, f"Dtype mismatch: {src_p.dtypes} vs {src_r.dtypes}"
            assert src_p.nodata == src_r.nodata, f"Nodata mismatch: {src_p.nodata} vs {src_r.nodata}"

            produced_res = (abs(src_p.transform.a), abs(src_p.transform.e))
            reference_res = (abs(src_r.transform.a), abs(src_r.transform.e))
            assert np.allclose(produced_res, reference_res), (
                f"Resolution mismatch: produced {produced_res} vs reference {reference_res}"
            )

            overlap_bounds = (
                max(src_p.bounds.left, src_r.bounds.left),
                max(src_p.bounds.bottom, src_r.bounds.bottom),
                min(src_p.bounds.right, src_r.bounds.right),
                min(src_p.bounds.top, src_r.bounds.top),
            )
            assert overlap_bounds[0] < overlap_bounds[2] and overlap_bounds[1] < overlap_bounds[3], (
                f"No overlapping raster extent between:\n  produced:  {produced_path}\n  reference: {reference_path}"
            )

            produced_window = from_bounds(*overlap_bounds, transform=src_p.transform).round_offsets().round_lengths()
            reference_window = from_bounds(*overlap_bounds, transform=src_r.transform).round_offsets().round_lengths()
            data_p = src_p.read(window=produced_window)
            data_r = src_r.read(window=reference_window)

            overlap_pixels = min(data_p.shape[-2], data_r.shape[-2]) * min(data_p.shape[-1], data_r.shape[-1])
            reference_pixels = src_r.width * src_r.height
            overlap_ratio = overlap_pixels / reference_pixels if reference_pixels else 0.0
            assert overlap_ratio >= MIN_E2E_OVERLAP_RATIO, (
                f"Overlap too small ({overlap_ratio:.1%}) between:\n"
                f"  produced:  {produced_path}\n  reference: {reference_path}"
            )

            if data_p.shape != data_r.shape:
                bands = min(data_p.shape[0], data_r.shape[0])
                height = min(data_p.shape[1], data_r.shape[1])
                width = min(data_p.shape[2], data_r.shape[2])
                data_p = data_p[:bands, :height, :width]
                data_r = data_r[:bands, :height, :width]

            nodata = src_p.nodata
            if nodata is not None:
                if np.isnan(nodata):
                    data_p = np.where(np.isnan(data_p), 0, data_p)
                    data_r = np.where(np.isnan(data_r), 0, data_r)
                else:
                    data_p = np.where(data_p == nodata, 0, data_p)
                    data_r = np.where(data_r == nodata, 0, data_r)

            mismatch_ratio = float(np.mean(data_p != data_r))
            mean_abs_diff = float(np.abs(data_p.astype(np.int32) - data_r.astype(np.int32)).mean())
            reference_active = data_r > 0
            produced_active = data_p > 0
            active_pixels = int(reference_active.sum())
            active_recall = float((produced_active & reference_active).sum() / active_pixels) if active_pixels else 1.0
            metrics = RasterComparisonMetrics(
                overlap_ratio=overlap_ratio,
                mismatch_ratio=mismatch_ratio,
                mean_abs_diff=mean_abs_diff,
                active_recall=active_recall,
                overlap_shape=tuple(int(size) for size in data_p.shape),
            )

            assert mismatch_ratio <= MAX_E2E_MISMATCH_RATIO, (
                f"Overlap mismatch ratio too high ({mismatch_ratio:.1%}) between:\n"
                f"  produced:  {produced_path}\n  reference: {reference_path}\n"
                f"  Overlap shape: {data_p.shape}, mean abs diff: {mean_abs_diff:.3f}"
            )
            assert mean_abs_diff <= MAX_E2E_MEAN_ABS_DIFF, (
                f"Mean absolute pixel drift too high ({mean_abs_diff:.3f}) between:\n"
                f"  produced:  {produced_path}\n  reference: {reference_path}"
            )
            assert active_recall >= MIN_E2E_ACTIVE_RECALL, (
                f"Flood-footprint recall too low ({active_recall:.1%}) between:\n"
                f"  produced:  {produced_path}\n  reference: {reference_path}"
            )

        if require_byte_identity:
            produced_hash, produced_size = _sha256_and_size(produced)
            reference_hash, reference_size = _sha256_and_size(reference)
            assert produced_size == reference_size, (
                f"Exact reference size mismatch: produced {produced_size} bytes vs reference {reference_size} bytes"
            )
            assert produced_hash == reference_hash, (
                f"Exact reference SHA256 mismatch: produced {produced_hash} vs reference {reference_hash}"
            )

    return metrics


def _artifact_paths(directory: Path, event_id: str, date_token: str) -> dict[str, Path]:
    """Build canonical artifact paths for the requested event/date."""
    layers = {**CLASSIFIED_LAYER_SUFFIXES, **RAW_LAYER_SUFFIXES}
    return {name: directory / f"{event_id}_{date_token}_{suffix}.tif" for name, suffix in layers.items()}


def _stats(data: np.ndarray) -> dict[str, int | None]:
    """Summarise valid byte values without exposing entire rasters in reports."""
    valid = data[data != GFM_NODATA]
    return {
        "total": int(data.size),
        "valid": int(valid.size),
        "nodata": int(data.size - valid.size),
        "zero": int((valid == 0).sum()),
        "positive": int((valid > 0).sum()),
        "min": int(valid.min()) if valid.size else None,
        "max": int(valid.max()) if valid.size else None,
        "unique": int(np.unique(valid).size),
    }


def _grid_is_canonical(transform: rasterio.Affine) -> bool:
    """Return whether the upper-left pixel edge aligns to the global 1' grid."""
    tol = GFM_RESOLUTION * 1e-6
    west_index = (transform.c + 180.0) / GFM_RESOLUTION
    north_index = (90.0 - transform.f) / GFM_RESOLUTION
    return abs(west_index - round(west_index)) <= tol and abs(north_index - round(north_index)) <= tol


def _domain_check(name: str, data: np.ndarray) -> tuple[bool, str]:
    """Validate layer-specific GFM code domains after removing nodata."""
    valid = data[data != GFM_NODATA]
    if name in {"flood_fraction", "water_fraction"}:
        allowed = (valid >= 0) & (valid <= 100)
    elif name == "ensemble_likelihood":
        allowed = (valid >= 0) & (valid <= 100)
    elif name in {"reference_water", "reference_water_mask"}:
        allowed = np.isin(valid, [0, 1, 2])
    elif name == "ensemble_flood_extent":
        allowed = np.isin(valid, [0, 1])
    else:
        return True, "native codes retained without a narrowed domain"
    passed = bool(allowed.all())
    message = "valid values match the declared GFM layer domain" if passed else "invalid values found"
    return passed, message


def _check_raw_diagnostics(data: dict[str, np.ndarray]) -> ValidationCheck:
    """Report cross-run raw/classified plausibility without asserting equality."""
    flood = data["flood_fraction"]
    water = data["water_fraction"]
    raw_flood = data["ensemble_flood_extent"]
    reference = data["reference_water"]
    raw_reference = data["reference_water_mask"]

    joint_flood = (flood != GFM_NODATA) & (raw_flood != GFM_NODATA)
    joint_reference = (reference != GFM_NODATA) & (raw_reference != GFM_NODATA)
    classified_positive = flood > 0
    raw_positive = raw_flood > 0
    details = {
        "joint_flood_pixels": int(joint_flood.sum()),
        "classified_flood_positive": int((classified_positive & joint_flood).sum()),
        "raw_flood_positive": int((raw_positive & joint_flood).sum()),
        "flood_intersection": int((classified_positive & raw_positive & joint_flood).sum()),
        "raw_positive_zero_classified": int((raw_positive & (flood == 0) & joint_flood).sum()),
        "flood_greater_than_water": int(((flood > water) & (flood != GFM_NODATA) & (water != GFM_NODATA)).sum()),
        "joint_reference_pixels": int(joint_reference.sum()),
        "reference_code_agreement": int(((reference == raw_reference) & joint_reference).sum()),
    }
    return ValidationCheck(
        name="raw_classified_diagnostics",
        passed=True,
        required=False,
        message=(
            "Informational only: classified fractions use averaged observation coverage while raw layers use "
            "nearest native codes and may originate from separate live-STAC reads."
        ),
        details=details,
    )


def validate_gfm_artifacts(
    input_dir: Path | str,
    *,
    event_id: str = "Valencia_2024",
    date_token: str = "2024-11-01",
    reference_base: str = DEFAULT_REFERENCE_BASE,
    require_raw: bool = False,
    strict_reference_bytes: bool = False,
) -> GfmValidationReport:
    """Validate one date's classified and optional raw harmonised GFM artifacts."""
    directory = Path(input_dir)
    paths = _artifact_paths(directory, event_id, date_token)
    checks: list[ValidationCheck] = []
    classified_names = set(CLASSIFIED_LAYER_SUFFIXES)
    raw_names = set(RAW_LAYER_SUFFIXES)
    missing_classified = sorted(name for name in classified_names if not paths[name].is_file())
    present_raw = {name for name in raw_names if paths[name].is_file()}
    missing_raw = sorted(raw_names - present_raw)

    checks.append(
        ValidationCheck(
            name="classified_artifacts",
            passed=not missing_classified,
            message=(
                "All six classified artifacts are present" if not missing_classified else "Missing classified artifacts"
            ),
            details={"missing": missing_classified},
        )
    )
    raw_complete = len(present_raw) == len(raw_names)
    raw_required = require_raw or bool(present_raw)
    if raw_required:
        checks.append(
            ValidationCheck(
                name="raw_artifacts",
                passed=raw_complete,
                message="Both raw artifacts are present" if raw_complete else "Raw artifact pair is incomplete",
                details={"present": sorted(present_raw), "missing": missing_raw},
            )
        )

    active_names = classified_names | (raw_names if raw_complete else set())
    available_names = [name for name in sorted(active_names) if paths[name].is_file()]
    arrays: dict[str, np.ndarray] = {}
    profiles: dict[str, dict[str, Any]] = {}
    for name in available_names:
        path = paths[name]
        try:
            with rasterio.open(path) as dataset:
                data = dataset.read(1)
                arrays[name] = data
                profile = {
                    "count": dataset.count,
                    "dtype": dataset.dtypes[0],
                    "nodata": dataset.nodata,
                    "crs": dataset.crs,
                    "shape": (dataset.height, dataset.width),
                    "transform": dataset.transform,
                    "bounds": dataset.bounds,
                    "res": dataset.res,
                }
                profiles[name] = profile
        except Exception as exc:
            checks.append(
                ValidationCheck(
                    name=f"read_{name}",
                    passed=False,
                    message=f"Could not read {path.name}: {exc}",
                )
            )
            continue

        profile_ok = (
            profile["count"] == 1
            and profile["dtype"] == "uint8"
            and profile["nodata"] == GFM_NODATA
            and profile["crs"] is not None
            and profile["crs"].to_epsg() == 4326
            and np.allclose(tuple(abs(value) for value in profile["res"]), (GFM_RESOLUTION, GFM_RESOLUTION))
            and _grid_is_canonical(profile["transform"])
        )
        checks.append(
            ValidationCheck(
                name=f"profile_{name}",
                passed=profile_ok,
                message=(
                    "Raster has the expected harmonised GFM profile"
                    if profile_ok
                    else "Raster profile is not canonical"
                ),
                details={
                    "path": str(path),
                    "count": profile["count"],
                    "dtype": profile["dtype"],
                    "nodata": profile["nodata"],
                    "crs": str(profile["crs"]),
                    "shape": profile["shape"],
                    "res": profile["res"],
                },
            )
        )
        domain_ok, domain_message = _domain_check(name, data)
        checks.append(
            ValidationCheck(
                name=f"domain_{name}",
                passed=domain_ok,
                message=domain_message,
                details=_stats(data),
            )
        )

    if profiles:
        reference_name = next(iter(profiles))
        reference_profile = profiles[reference_name]
        differing = [
            name
            for name, profile in profiles.items()
            if profile["shape"] != reference_profile["shape"]
            or profile["transform"] != reference_profile["transform"]
            or profile["crs"] != reference_profile["crs"]
            or profile["nodata"] != reference_profile["nodata"]
        ]
        checks.append(
            ValidationCheck(
                name="shared_grid",
                passed=not differing and len(profiles) == len(available_names),
                message=(
                    "All artifacts share one canonical grid" if not differing else "Artifacts do not share one grid"
                ),
                details={"reference": reference_name, "differing": differing},
            )
        )

    classified_diagnostic_layers = {"flood_fraction", "water_fraction", "reference_water"}
    if raw_complete and raw_names.issubset(arrays) and classified_diagnostic_layers.issubset(arrays):
        checks.append(_check_raw_diagnostics(arrays))

    for name in sorted(classified_names):
        path = paths[name]
        if not path.is_file():
            continue
        reference_path = f"{reference_base.rstrip('/')}/{path.name}"
        try:
            metrics = compare_rasters(path, reference_path, require_byte_identity=strict_reference_bytes)
            checks.append(
                ValidationCheck(
                    name=f"reference_{name}",
                    passed=True,
                    message="Matches the S3 reference within the configured tolerance",
                    details={
                        "produced": str(path),
                        "reference": reference_path,
                        "overlap_ratio": metrics.overlap_ratio,
                        "mismatch_ratio": metrics.mismatch_ratio,
                        "mean_abs_diff": metrics.mean_abs_diff,
                        "active_recall": metrics.active_recall,
                        "overlap_shape": metrics.overlap_shape,
                    },
                )
            )
        except Exception as exc:
            checks.append(
                ValidationCheck(
                    name=f"reference_{name}",
                    passed=False,
                    message=f"Reference comparison failed: {exc}",
                    details={"produced": str(path), "reference": reference_path},
                )
            )

    return GfmValidationReport(checks=checks)
