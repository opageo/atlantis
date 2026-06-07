"""End-to-end tests for the GFM pipeline.

Compare pipeline outputs (harmonised GeoTIFFs) byte-for-byte against
reference files stored on S3 at:
    s3://atlantis/reference/Valencia_2024_gfm/gfm/harmonised/

These tests require:
    - Network access to the EODC STAC API (GFM data)
    - Network access to AWS S3 (reference files)

Run with:
    uv run python -m pytest tests/fetchers/test_gfm_e2e.py -v
"""

from __future__ import annotations

import subprocess

import boto3
import numpy as np
import pytest
import rasterio
from rasterio.session import AWSSession
from upath import UPath

S3_REFERENCE_BASE = "s3://atlantis/reference/Valencia_2024_gfm/gfm/harmonised"

# Event parameters matching the reference run
EVENT_ID = "Valencia_2024"
BBOX = "-1.5 38.8 0.5 40.0"
START_DATE = "2024-10-29"
END_DATE = "2024-11-04"

# Expected reference filenames per strategy
REFERENCE_FILES = {
    "peak": "Valencia_2024_20241031_gfm_harmonised.tif",
    "aggregate": "Valencia_2024_20241030_20241101_gfm_harmonised.tif",
}


def _run_gfm_pipeline(strategy: str, output_dir: UPath) -> list[UPath]:
    """Run the GFM fetch pipeline via CLI and return harmonised TIF paths."""
    cmd = [
        "uv",
        "run",
        "atlantis",
        "fetch",
        "--event",
        EVENT_ID,
        "--source",
        "gfm",
        "--bbox",
        BBOX,
        "--start-date",
        START_DATE,
        "--end-date",
        END_DATE,
        "--strategy",
        strategy,
        "--harmonise",
        "--output",
        str(output_dir),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        pytest.fail(
            f"GFM pipeline failed (strategy={strategy}):\n"
            f"stdout: {result.stdout[-2000:]}\n"
            f"stderr: {result.stderr[-2000:]}"
        )

    harm_dir = output_dir / "gfm" / "harmonised"
    tifs = sorted(harm_dir.glob("*_gfm_harmonised.tif"))
    if not tifs:
        pytest.fail(
            f"No harmonised TIFs produced (strategy={strategy}).\nOutput dir contents: {list(output_dir.rglob('*'))}"
        )
    return tifs


def _compare_rasters(produced: UPath, reference: UPath) -> None:
    """Compare two GeoTIFFs: metadata + pixel data must match exactly."""
    with rasterio.open(produced.as_posix()) as src_p, rasterio.open(reference.as_uri()) as src_r:
        # Check spatial metadata
        assert src_p.crs == src_r.crs, f"CRS mismatch: {src_p.crs} vs {src_r.crs}"
        assert src_p.width == src_r.width, f"Width mismatch: {src_p.width} vs {src_r.width}"
        assert src_p.height == src_r.height, f"Height mismatch: {src_p.height} vs {src_r.height}"
        assert src_p.transform == src_r.transform, (
            f"Transform mismatch:\n  produced:  {src_p.transform}\n  reference: {src_r.transform}"
        )
        assert src_p.dtypes == src_r.dtypes, f"Dtype mismatch: {src_p.dtypes} vs {src_r.dtypes}"
        assert src_p.nodata == src_r.nodata, f"Nodata mismatch: {src_p.nodata} vs {src_r.nodata}"

        # Binary pixel comparison
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


@pytest.mark.e2e
class TestGfmE2EPeak:
    """End-to-end test: GFM pipeline with --strategy peak."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.tmp_path = tmp_path

    def test_peak_matches_reference(self):
        """Pipeline output with strategy=peak matches the S3 reference byte-for-byte."""
        reference_file = UPath(S3_REFERENCE_BASE) / REFERENCE_FILES["peak"]

        # Run pipeline
        output_dir = self.tmp_path / "output"
        tifs = _run_gfm_pipeline("peak", output_dir)

        # Find the matching output file
        produced = None
        for tif in tifs:
            if tif.name == reference_file.name:
                produced = tif
                break

        if produced is None:
            # The peak date may vary if data availability changes;
            # compare whichever single file was produced
            assert len(tifs) == 1, f"Peak strategy should produce exactly 1 file, got {len(tifs)}: {tifs}"
            produced = tifs[0]

        boto3_session = boto3.Session(profile_name="default")
        aws_s3_endpoint = boto3_session.client("s3").meta.endpoint_url.removeprefix("https://")

        with rasterio.Env(
            AWSSession(boto3_session),
            AWS_S3_ENDPOINT=aws_s3_endpoint,
            AWS_HTTPS="YES",
            AWS_VIRTUAL_HOSTING="FALSE",
        ):
            _compare_rasters(produced, reference_file)


@pytest.mark.e2e
class TestGfmE2EAggregate:
    """End-to-end test: GFM pipeline with --strategy aggregate."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.tmp_path = tmp_path

    def test_aggregate_matches_reference(self):
        """Pipeline output with strategy=aggregate matches the S3 reference byte-for-byte."""

        reference_file = UPath(S3_REFERENCE_BASE) / REFERENCE_FILES["aggregate"]

        # Run pipeline
        output_dir = self.tmp_path / "output"
        tifs = _run_gfm_pipeline("aggregate", output_dir)

        # Find the matching output file
        produced = None
        for tif in tifs:
            if tif.name == reference_file.name:
                produced = tif
                break

        if produced is None:
            assert len(tifs) == 1, f"Aggregate strategy should produce exactly 1 file, got {len(tifs)}: {tifs}"
            produced = tifs[0]

        boto3_session = boto3.Session(profile_name="default")
        aws_s3_endpoint = boto3_session.client("s3").meta.endpoint_url.removeprefix("https://")

        with rasterio.Env(
            AWSSession(boto3_session),
            AWS_S3_ENDPOINT=aws_s3_endpoint,
            AWS_HTTPS="YES",
            AWS_VIRTUAL_HOSTING="FALSE",
        ):
            _compare_rasters(produced, reference_file)
