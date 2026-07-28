"""Unit tests for gfm/batch_processor.py — offline, fixture-based.

``GfmRasterProcessor.process_items`` is mocked (it needs live STAC/COG
access), but the rest of the pipeline — ``processed_tile_to_dataset``,
``Harmoniser.harmonise``, and the uint8 encode step — runs for real against a
synthetic ``GfmProcessedTile``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pytest
from rasterio.transform import from_bounds

from atlantis.fetchers.gfm.processor import GfmProcessedTile, GfmProcessResult
from atlantis.models.metadata import TileMetadata

_BBOX = (-1.5, 38.8, 0.5, 40.0)


def _make_process_result(rows: int = 32, cols: int = 32) -> GfmProcessResult:
    transform = from_bounds(*_BBOX, cols, rows)

    water_fraction = np.full((rows, cols), 0.3, dtype="float32")
    water_fraction[0:4, 0:4] = np.nan  # unobserved corner

    reference_water = np.zeros((rows, cols), dtype="uint8")
    reference_water[10:14, 10:14] = 1  # permanent water
    reference_water[0:4, 0:4] = 255  # nodata, matching the unobserved corner

    exclusion_mask = np.zeros((rows, cols), dtype="uint8")
    exclusion_mask[0:4, 0:4] = 3  # native GFM exclusion code (not synthesized)

    processed = GfmProcessedTile(
        transform=transform,
        crs="EPSG:4326",
        shape=(rows, cols),
        cloud_fraction=0.05,
        water_fraction=water_fraction,
        reference_water=reference_water,
        extra_layers={"exclusion_mask": exclusion_mask},
    )
    metadata = TileMetadata(
        event_id="",
        source_id="gfm",
        fetch_timestamp=datetime.now(timezone.utc),
        bbox=_BBOX,
    )
    return GfmProcessResult(processed=processed, paths=None, metadata=metadata)


@pytest.fixture()
def sample_task():
    return {
        "task_id": "gfm-20241101-EU020M_E036N009T3",
        "date": "2024-11-01",
        "equi7_tile": "EU020M_E036N009T3",
        "item_hrefs": [
            "https://stac.eodc.eu/api/v1/collections/GFM/items/a",
            "https://stac.eodc.eu/api/v1/collections/GFM/items/b",
        ],
        "bbox": _BBOX,
    }


def test_harmonise_gfm_payload_returns_cube_layers(sample_task):
    import atlantis.fetchers.gfm.batch_processor as bp

    with (
        patch("pystac.Item.from_file", side_effect=[object(), object()]),
        patch.object(bp.GfmRasterProcessor, "process_items", return_value=_make_process_result()),
    ):
        payload = bp.harmonise_gfm_payload(sample_task)

    expected = {"task_id", "date", "equi7_tile", "water_fraction", "exclusion_mask", "reference_water", "y", "x"}
    assert expected.issubset(payload.keys())
    assert payload["task_id"] == sample_task["task_id"]
    assert payload["equi7_tile"] == "EU020M_E036N009T3"
    assert payload["date"] == "2024-11-01"

    water = payload["water_fraction"]
    for key in ("exclusion_mask", "reference_water"):
        arr = payload[key]
        assert arr.shape == water.shape
        assert arr.dtype == np.uint8

    # Only the shared cube layers are returned — companions and flood_fraction
    # are not part of the batch/cube schema (see docs/layers.md).
    assert "ensemble_likelihood" not in payload
    assert "advisory_flags" not in payload
    assert "flood_fraction" not in payload


def test_harmonise_gfm_payload_fetches_every_item_href(sample_task):
    import atlantis.fetchers.gfm.batch_processor as bp

    with (
        patch("pystac.Item.from_file", side_effect=[object(), object()]) as mock_from_file,
        patch.object(bp.GfmRasterProcessor, "process_items", return_value=_make_process_result()),
    ):
        bp.harmonise_gfm_payload(sample_task)

    assert mock_from_file.call_count == len(sample_task["item_hrefs"])
    called_hrefs = [call.args[0] for call in mock_from_file.call_args_list]
    assert called_hrefs == sample_task["item_hrefs"]


def test_harmonise_gfm_payload_raises_when_no_data(sample_task):
    import atlantis.fetchers.gfm.batch_processor as bp

    with (
        patch("pystac.Item.from_file", side_effect=[object(), object()]),
        patch.object(bp.GfmRasterProcessor, "process_items", return_value=None),
    ):
        with pytest.raises(RuntimeError, match="No valid GFM data"):
            bp.harmonise_gfm_payload(sample_task)
