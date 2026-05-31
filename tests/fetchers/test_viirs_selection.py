"""Tests for VIIRS peak-date selection helpers."""

import numpy as np
from rasterio.transform import from_origin

from atlantis.fetchers.viirs.processor import ProcessedTile
from atlantis.fetchers.viirs.selection import flood_pixel_count, is_better_peak_candidate


def _processed(*, flood: np.ndarray | None = None, raw: np.ndarray | None = None) -> ProcessedTile:
    transform = from_origin(0.0, 1.0, 1.0, -1.0)
    return ProcessedTile(
        transform=transform,
        crs="EPSG:4326",
        cloud_fraction=0.0,
        raw=raw,
        flood_extent=flood,
    )


class TestFloodPixelCount:
    def test_classified_counts_positive_pixels(self):
        flood = np.array([[0, 1], [1, 0]], dtype=np.uint8)
        assert flood_pixel_count(_processed(flood=flood)) == 2

    def test_raw_counts_flood_code_range(self):
        raw = np.array([[0, 150], [170, 50]], dtype=np.uint8)
        assert flood_pixel_count(_processed(raw=raw)) == 2


class TestIsBetterPeakCandidate:
    def test_strictly_greater(self):
        assert is_better_peak_candidate(5, 4) is True
        assert is_better_peak_candidate(4, 4) is False
