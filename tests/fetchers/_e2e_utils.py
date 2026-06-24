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
from typer.testing import CliRunner
from upath import UPath

from atlantis.cli import cli


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
    """Compare two GeoTIFFs: metadata + pixel data must match exactly."""
    with rasterio.open(produced.as_posix()) as src_p, rasterio.open(reference.as_uri()) as src_r:
        assert src_p.crs == src_r.crs, f"CRS mismatch: {src_p.crs} vs {src_r.crs}"
        assert src_p.width == src_r.width, f"Width mismatch: {src_p.width} vs {src_r.width}"
        assert src_p.height == src_r.height, f"Height mismatch: {src_p.height} vs {src_r.height}"
        assert src_p.transform == src_r.transform, (
            f"Transform mismatch:\n  produced:  {src_p.transform}\n  reference: {src_r.transform}"
        )
        assert src_p.dtypes == src_r.dtypes, f"Dtype mismatch: {src_p.dtypes} vs {src_r.dtypes}"
        assert src_p.nodata == src_r.nodata, f"Nodata mismatch: {src_p.nodata} vs {src_r.nodata}"

        data_p = src_p.read()
        data_r = src_r.read()
        np.testing.assert_array_equal(
            data_p,
            data_r,
            err_msg=(
                f"Pixel data mismatch between:\n"
                f"  produced:  {produced}\n"
                f"  reference: {reference}\n"
                f"  Shape: {data_p.shape}, dtype: {data_p.dtype}\n"
                f"  Differing pixels: {int(np.sum(data_p != data_r))}"
            ),
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
