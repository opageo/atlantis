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

# NOTE this e2e test cross-check the viirs-demo cli case from makefile, the strategy parameters are defined at
# _e2e_utils.py, if you want to add more strategies this needs to be refactored

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

LAYERS_RASTERS = [
    "Valencia_2024_2024-11-02_viirs_cloud_mask_harmonised.tif",
    "Valencia_2024_2024-11-02_viirs_exclusion_mask_harmonised.tif",
    "Valencia_2024_2024-11-02_viirs_harmonised.tif",
    "Valencia_2024_2024-11-02_viirs_reference_water_harmonised.tif",
    "Valencia_2024_2024-11-02_viirs_shadow_harmonised.tif",
    "Valencia_2024_2024-11-02_viirs_snow_ice_harmonised.tif",
    "Valencia_2024_2024-11-02_viirs_water_fraction_harmonised.tif",
]


@pytest.fixture(scope="class")
def produced_tifs(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> list[UPath]:
    output_dir = UPath(tmp_path_factory.mktemp("output"))

    strategy = request.cls.strategy

    return _run_viirs_pipeline(strategy, output_dir)


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
class TestViirsE2EAll:
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
                require_byte_identity=True,
            )
