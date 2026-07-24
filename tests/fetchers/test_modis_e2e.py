"""End-to-end tests for the MODIS pipeline (Valencia 2024).

Compare pipeline outputs (harmonised GeoTIFFs) against reference files stored
on S3 at:
    s3://atlantis/reference/Valencia_2024_modis/modis/harmonised/

By default the raster comparison tolerates small live-data drift on the
overlapping aligned grid. Set ATLANTIS_E2E_STRICT_REFERENCE_BYTES=1 to also
require exact file identity against the stored reference object.

These tests require:
    - EARTHDATA_TOKEN environment variable (LAADS DAAC access)
    - Network access to LAADS DAAC (MODIS HDF4 data)
    - Network access to AWS S3 (reference files)

Run with:
    uv run python -m pytest tests/fetchers/test_modis_e2e.py -v -m e2e
    ATLANTIS_E2E_STRICT_REFERENCE_BYTES=1 uv run python -m pytest tests/fetchers/test_modis_e2e.py -v -m e2e

Reference created with:
uv run atlantis --verbose fetch \
        --event Valencia_2024 --source modis \
        --bbox "-1.5 38.8 0.5 40.0" \
        --start-date 2024-10-29 --end-date 2024-11-04 \
        --modis-backend laads_hdf4 --modis-composite F2 --strategy all --peak-window-days 2 --max-observations 3 --peak-priority balanced --harmonise --no-keep-processed \
        --output ./data/Valencia_2024
"""  # noqa: E501

from __future__ import annotations

import os

import pytest
from upath import UPath

from tests.fetchers._e2e_utils import compare_rasters, run_pipeline, s3_rasterio_env, strict_reference_bytes_enabled

S3_REFERENCE_BASE = "s3://atlantis/reference/Valencia_2024_modis/"
STRICT_REFERENCE_BYTES = strict_reference_bytes_enabled()

# Event parameters matching the reference run
EVENT_ID = "Valencia_2024"
BBOX = "-1.5 38.8 0.5 40.0"
START_DATE = "2024-10-29"
END_DATE = "2024-11-04"

MODIS_EXTRA_ARGS = ["--modis-backend", "laads_hdf4", "--modis-composite", "F2"]

LAYERS_RASTERS = [
    "Valencia_2024_2024-11-01_modis_exclusion_mask_harmonised.tif",
    "Valencia_2024_2024-11-01_modis_harmonised.tif",
    "Valencia_2024_2024-11-01_modis_recurring_flood_harmonised.tif",
    "Valencia_2024_2024-11-01_modis_reference_water_harmonised.tif",
    "Valencia_2024_2024-11-01_modis_water_fraction_harmonised.tif",
]


@pytest.fixture(scope="class")
def produced_tifs(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> list[UPath]:
    output_dir = UPath(tmp_path_factory.mktemp("output"))

    strategy = request.cls.strategy

    return _run_modis_pipeline(strategy, output_dir)


def _run_modis_pipeline(strategy: str, output_dir: UPath) -> list[UPath]:
    """Run the MODIS fetch pipeline and return harmonised TIF paths."""
    return run_pipeline(
        "modis",
        event_id=EVENT_ID,
        bbox=BBOX,
        start_date=START_DATE,
        end_date=END_DATE,
        strategy=strategy,
        output_dir=output_dir,
        extra_args=MODIS_EXTRA_ARGS,
    )


@pytest.mark.e2e
class TestModisE2EAll:
    if not os.getenv("EARTHDATA_TOKEN"):
        pytest.fail("You are lacking the EARTHDATA_TOKEN environment variable, which is required for MODIS tests.")
    strategy = "all"

    @pytest.mark.parametrize("layer_raster", LAYERS_RASTERS)
    def test_matches_reference(
        self,
        produced_tifs: list[UPath],
        layer_raster: str,
    ):
        reference_file = UPath(S3_REFERENCE_BASE) / f"strategy_{self.strategy}" / "harmonised" / layer_raster

        produced = next(
            (tif for tif in produced_tifs if tif.name == layer_raster),
            None,
        )

        assert produced is not None

        with s3_rasterio_env():
            compare_rasters(
                produced,
                reference_file,
                require_byte_identity=STRICT_REFERENCE_BYTES,
            )
