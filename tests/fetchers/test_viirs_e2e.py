"""End-to-end tests for the VIIRS pipeline (Valencia 2024).

Compare pipeline outputs (harmonised GeoTIFFs) against reference files stored
on S3 at:
    s3://atlantis/reference/Valencia_2024_viirs/viirs/harmonised/

By default the raster comparison tolerates small live-data drift on the
overlapping aligned grid. Set ATLANTIS_E2E_STRICT_REFERENCE_BYTES=1 to also
require exact file identity against the stored reference object.

These tests require:
    - Network access to NOAA S3 (VIIRS flood data)
    - Network access to AWS S3 (reference files)

Run with:
    uv run python -m pytest tests/fetchers/test_viirs_e2e.py -v -m e2e
    ATLANTIS_E2E_STRICT_REFERENCE_BYTES=1 uv run python -m pytest tests/fetchers/test_viirs_e2e.py -v -m e2e
"""

from __future__ import annotations

import pytest
from upath import UPath

from tests.fetchers._e2e_utils import compare_rasters, run_pipeline, s3_rasterio_env, strict_reference_bytes_enabled

S3_REFERENCE_BASE = "s3://atlantis/reference/Valencia_2024_viirs"
STRICT_REFERENCE_BYTES = strict_reference_bytes_enabled()

# Event parameters matching the reference run
EVENT_ID = "Valencia_2024"
BBOX = "-1.5 38.8 0.5 40.0"
START_DATE = "2024-10-29"
END_DATE = "2024-11-04"

VIIRS_EXTRA_ARGS = ["--viirs-backend", "noaa_s3"]

# Expected reference filenames per strategy
REFERENCE_FILES = {
    "peak": "Valencia_2024_2024-11-02_viirs_harmonised.tif",
    "aggregate": "Valencia_2024_aggregated_viirs_harmonised.tif",
}


def _run_viirs_pipeline(strategy: str, output_dir: UPath) -> list[UPath]:
    """Run the VIIRS fetch pipeline and return harmonised TIF paths."""
    return run_pipeline(
        "viirs",
        event_id=EVENT_ID,
        bbox=BBOX,
        start_date=START_DATE,
        end_date=END_DATE,
        strategy=strategy,
        output_dir=output_dir,
        extra_args=VIIRS_EXTRA_ARGS,
    )


@pytest.mark.e2e
class TestViirsE2EPeak:
    """End-to-end test: VIIRS pipeline with --strategy peak."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.tmp_path = tmp_path

    def test_peak_matches_reference(self):
        """Pipeline output matches the S3 reference raster, optionally by exact bytes."""
        reference_file = UPath(S3_REFERENCE_BASE) / REFERENCE_FILES["peak"]

        output_dir = UPath(self.tmp_path / "output")
        tifs = _run_viirs_pipeline("peak", output_dir)

        # Find the matching output file
        produced = None
        for tif in tifs:
            if tif.name == reference_file.name:
                produced = tif
                break

        if produced is None:
            assert len(tifs) == 1, f"Peak strategy should produce exactly 1 file, got {len(tifs)}: {tifs}"
            produced = tifs[0]

        with s3_rasterio_env():
            compare_rasters(produced, reference_file, require_byte_identity=STRICT_REFERENCE_BYTES)


@pytest.mark.e2e
class TestViirsE2EAggregate:
    """End-to-end test: VIIRS pipeline with --strategy aggregate."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.tmp_path = tmp_path

    def test_aggregate_matches_reference(self):
        """Pipeline output matches the S3 reference raster, optionally by exact bytes."""
        reference_file = UPath(S3_REFERENCE_BASE) / REFERENCE_FILES["aggregate"]

        output_dir = UPath(self.tmp_path / "output")
        tifs = _run_viirs_pipeline("aggregate", output_dir)

        produced = None
        for tif in tifs:
            if tif.name == reference_file.name:
                produced = tif
                break

        if produced is None:
            assert len(tifs) == 1, f"Aggregate strategy should produce exactly 1 file, got {len(tifs)}: {tifs}"
            produced = tifs[0]

        with s3_rasterio_env():
            compare_rasters(produced, reference_file, require_byte_identity=STRICT_REFERENCE_BYTES)
