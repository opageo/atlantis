"""Shared helpers for end-to-end pipeline tests.

Centralises the CLI runner invocation, raster comparison, and S3 rasterio
environment setup so individual e2e test modules stay short.

## Regenerating e2e references

Each source needs its reference harmonised TIFs uploaded once to the
``s3://atlantis/reference/`` bucket.  Example for MODIS:

    uv run atlantis fetch --event Valencia_2024 --source modis \
        --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
        --modis-backend laads_hdf4 --modis-composite F2 \
        --strategy peak --harmonise --no-keep-processed --output ./_ref/modis_peak
    uv run atlantis fetch ... --strategy aggregate ... --output ./_ref/modis_agg
    aws s3 cp ./_ref/modis_peak/modis/harmonised/ \
        s3://atlantis/reference/Valencia_2024_modis/modis/harmonised/ --recursive
    aws s3 cp ./_ref/modis_agg/modis/harmonised/ \
        s3://atlantis/reference/Valencia_2024_modis/modis/harmonised/ --recursive

Repeat similarly for VIIRS and GFM.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import boto3
import numpy as np
import pytest
import rasterio
from rasterio.session import AWSSession
from rasterio.windows import from_bounds
from typer.testing import CliRunner
from upath import UPath

from atlantis.cli import cli

MAX_E2E_MISMATCH_RATIO = 0.03
MAX_E2E_MEAN_ABS_DIFF = 0.5
MIN_E2E_ACTIVE_RECALL = 0.30
MIN_E2E_OVERLAP_RATIO = 0.95


def run_pipeline(
    source: str,
    *,
    event_id: str,
    bbox: str,
    start_date: str,
    end_date: str,
    strategy: str,
    output_dir: UPath,
    extra_args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> list[UPath]:
    """Run a fetch pipeline via the CLI and return harmonised TIF paths.

    Parameters
    ----------
    source:
        Data source (``gfm``, ``modis``, ``viirs``).
    extra_args:
        Additional CLI flags specific to the source, e.g.
        ``["--modis-backend", "laads_hdf4", "--modis-composite", "F2"]``.
    env:
        Extra environment variables to pass to the CliRunner.
    """
    runner = CliRunner(env=env)
    args = [
        "--verbose",
        "fetch",
        "--event",
        event_id,
        "--source",
        source,
        "--bbox",
        bbox,
        "--start-date",
        start_date,
        "--end-date",
        end_date,
        "--strategy",
        strategy,
        "--peak-window-days",
        "2",
        "--max-observations",
        "3",
        "--peak-priority",
        "balanced",
        "--harmonise",
        "--no-keep-processed",
        "--output",
        str(output_dir),
    ]
    if extra_args:
        args.extend(extra_args)

    result = runner.invoke(cli, args)
    if result.exit_code != 0:
        output = result.stdout if result.stdout else ""
        exc = f"\n{result.exception}" if result.exception else ""
        pytest.fail(f"{source} pipeline failed (strategy={strategy}):\noutput: {output[-2000:]}{exc}")

    harm_dir = output_dir / source / "harmonised"
    tifs = sorted(harm_dir.glob(f"*_{source}_harmonised.tif"))
    if not tifs:
        pytest.fail(
            f"No harmonised TIFs produced (source={source}, strategy={strategy}).\n"
            f"Output dir contents: {list(output_dir.rglob('*'))}"
        )
    return tifs


def compare_rasters(produced: UPath, reference: UPath) -> None:
    """Compare two GeoTIFFs on their shared grid with small drift tolerance.

    These e2e tests exercise live upstream sources plus canonical reference
    rasters stored on S3. Small differences can arise from source-side updates,
    edge-pixel nodata handling, or a one-pixel expansion to the snapped global
    grid. We therefore compare the overlapping aligned window rather than
    requiring byte-for-byte identity across the full raster extent.
    """
    with rasterio.open(produced.as_posix()) as src_p, rasterio.open(reference.as_uri()) as src_r:
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
            f"No overlapping raster extent between:\n  produced:  {produced}\n  reference: {reference}"
        )

        produced_window = from_bounds(*overlap_bounds, transform=src_p.transform).round_offsets().round_lengths()
        reference_window = from_bounds(*overlap_bounds, transform=src_r.transform).round_offsets().round_lengths()
        data_p = src_p.read(window=produced_window)
        data_r = src_r.read(window=reference_window)

        overlap_pixels = min(data_p.shape[-2], data_r.shape[-2]) * min(data_p.shape[-1], data_r.shape[-1])
        reference_pixels = src_r.width * src_r.height
        overlap_ratio = overlap_pixels / reference_pixels if reference_pixels else 0.0
        assert overlap_ratio >= MIN_E2E_OVERLAP_RATIO, (
            f"Overlap too small ({overlap_ratio:.1%}) between:\n  produced:  {produced}\n  reference: {reference}"
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

        assert mismatch_ratio <= MAX_E2E_MISMATCH_RATIO, (
            f"Overlap mismatch ratio too high ({mismatch_ratio:.1%}) between:\n"
            f"  produced:  {produced}\n"
            f"  reference: {reference}\n"
            f"  Overlap shape: {data_p.shape}, mean abs diff: {mean_abs_diff:.3f}"
        )
        assert mean_abs_diff <= MAX_E2E_MEAN_ABS_DIFF, (
            f"Mean absolute pixel drift too high ({mean_abs_diff:.3f}) between:\n"
            f"  produced:  {produced}\n"
            f"  reference: {reference}"
        )
        assert active_recall >= MIN_E2E_ACTIVE_RECALL, (
            f"Flood-footprint recall too low ({active_recall:.1%}) between:\n"
            f"  produced:  {produced}\n"
            f"  reference: {reference}"
        )


@contextmanager
def s3_rasterio_env() -> Iterator[None]:
    """Context manager that configures rasterio to read from the atlantis S3 bucket."""
    boto3_session = boto3.Session(profile_name="default")
    aws_s3_endpoint = boto3_session.client("s3").meta.endpoint_url.removeprefix("https://")

    with rasterio.Env(
        AWSSession(boto3_session),
        AWS_S3_ENDPOINT=aws_s3_endpoint,
        AWS_HTTPS="YES",
        AWS_VIRTUAL_HOSTING="FALSE",
    ):
        yield
