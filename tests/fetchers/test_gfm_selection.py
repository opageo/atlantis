"""Tests for GFM peak-date selection helpers (parity with VIIRS / MODIS)."""

from __future__ import annotations

import numpy as np
import pytest
from rasterio.transform import from_origin

from atlantis.fetchers.gfm.processor import GFM_FLOOD, GFM_NODATA, GfmProcessedTile
from atlantis.fetchers.gfm.selection import (
    _parse_yyyymmdd,
    flood_pixel_count,
    is_better_peak_candidate,
    select_peak_window,
    subsample_around_peak,
)


def _make_flood_tile(n_flood_pixels: int) -> GfmProcessedTile:
    """Synthetic GfmProcessedTile with *n_flood_pixels* positive cells."""
    transform = from_origin(0.0, 1.0, 1.0, -1.0)
    data = np.zeros((4, 4), dtype=np.float32)
    data.ravel()[:n_flood_pixels] = 0.7
    quality = np.ones_like(data, dtype=np.uint8)
    permanent = np.zeros_like(data, dtype=np.uint8)
    return GfmProcessedTile(
        flood_fraction=data,
        quality_mask=quality,
        permanent_water=permanent,
        transform=transform,
        crs="EPSG:4326",
        shape=data.shape,
        cloud_fraction=0.0,
    )


# ── flood_pixel_count / is_better_peak_candidate ─────────────────────────────


class TestFloodPixelCount:
    def test_counts_positive_pixels(self):
        assert flood_pixel_count(_make_flood_tile(5)) == 5

    def test_ignores_nan(self):
        tile = _make_flood_tile(3)
        tile.flood_fraction[3, 3] = np.nan
        assert flood_pixel_count(tile) == 3

    # Native / raw mode fallback
    def test_native_mode_counts_flood_code(self):
        """flood_pixel_count falls back to ensemble_flood_extent == GFM_FLOOD."""
        transform = from_origin(0.0, 1.0, 1.0, -1.0)
        efe = np.array([[GFM_FLOOD, 0, GFM_NODATA], [0, GFM_FLOOD, GFM_FLOOD]], dtype=np.uint8)
        tile = GfmProcessedTile(
            ensemble_flood_extent=efe,
            reference_water_mask=np.zeros((2, 3), dtype=np.uint8),
            transform=transform,
            crs="EPSG:4326",
            shape=(2, 3),
        )
        assert flood_pixel_count(tile) == 3  # three pixels with code 1 (and not nodata)

    def test_native_mode_empty_returns_zero(self):
        """Returns 0 when no bands are populated."""
        transform = from_origin(0.0, 1.0, 1.0, -1.0)
        tile = GfmProcessedTile(transform=transform, crs="EPSG:4326", shape=(2, 2))
        assert flood_pixel_count(tile) == 0


class TestIsBetterPeakCandidate:
    def test_strictly_greater(self):
        assert is_better_peak_candidate(5, 4) is True
        assert is_better_peak_candidate(4, 4) is False


# ── _parse_yyyymmdd ──────────────────────────────────────────────────────────


class TestParseYyyymmdd:
    def test_valid_token(self):
        from datetime import date

        assert _parse_yyyymmdd("20200722") == date(2020, 7, 22)

    def test_non_date_returns_none(self):
        assert _parse_yyyymmdd("aggregated") is None
        assert _parse_yyyymmdd("2020072") is None
        assert _parse_yyyymmdd("202007220") is None


# ── select_peak_window ───────────────────────────────────────────────────────


class TestSelectPeakWindow:
    def _five_date_map(self):
        tokens = ["20200701", "20200702", "20200703", "20200704", "20200705"]
        counts = [2, 5, 16, 8, 3]
        return tokens, {t: _make_flood_tile(c) for t, c in zip(tokens, counts)}

    def test_zero_window_returns_all(self):
        tokens, pmap = self._five_date_map()
        assert select_peak_window(tokens, pmap, days_before=0, days_after=0) == tokens

    def test_symmetric_window_filters_correctly(self):
        tokens, pmap = self._five_date_map()
        # Peak = day 3. ±1 day → days 2, 3, 4
        assert select_peak_window(tokens, pmap, days_before=1, days_after=1) == [
            "20200702",
            "20200703",
            "20200704",
        ]

    def test_asymmetric_before_only(self):
        tokens, pmap = self._five_date_map()
        assert select_peak_window(tokens, pmap, days_before=2, days_after=0) == [
            "20200701",
            "20200702",
            "20200703",
        ]

    def test_asymmetric_after_only(self):
        tokens, pmap = self._five_date_map()
        assert select_peak_window(tokens, pmap, days_before=0, days_after=2) == [
            "20200703",
            "20200704",
            "20200705",
        ]

    def test_window_larger_than_range_returns_all(self):
        tokens, pmap = self._five_date_map()
        assert select_peak_window(tokens, pmap, days_before=10, days_after=10) == tokens

    def test_non_date_tokens_excluded(self):
        tokens, pmap = self._five_date_map()
        tokens = ["aggregated"] + tokens
        pmap["aggregated"] = _make_flood_tile(100)  # would be peak but excluded
        result = select_peak_window(tokens, pmap, days_before=1, days_after=1)
        assert "aggregated" not in result
        assert result == ["20200702", "20200703", "20200704"]

    def test_negative_days_raises(self):
        tokens, pmap = self._five_date_map()
        with pytest.raises(ValueError):
            select_peak_window(tokens, pmap, days_before=-1, days_after=0)
        with pytest.raises(ValueError):
            select_peak_window(tokens, pmap, days_before=0, days_after=-1)


# ── subsample_around_peak ────────────────────────────────────────────────────


class TestSubsampleAroundPeak:
    def _tokens(self):
        return ["20200701", "20200702", "20200703", "20200704", "20200705"]

    def test_no_limit_returns_all(self):
        tokens = self._tokens()
        assert subsample_around_peak(tokens, "20200703", max_observations=0) == tokens

    def test_max_one_returns_only_peak(self):
        tokens = self._tokens()
        assert subsample_around_peak(tokens, "20200703", max_observations=1) == ["20200703"]

    def test_post_priority(self):
        tokens = self._tokens()
        assert subsample_around_peak(tokens, "20200703", 3, "post") == [
            "20200703",
            "20200704",
            "20200705",
        ]

    def test_pre_priority(self):
        tokens = self._tokens()
        assert subsample_around_peak(tokens, "20200703", 3, "pre") == [
            "20200701",
            "20200702",
            "20200703",
        ]

    def test_balanced_priority(self):
        tokens = self._tokens()
        assert subsample_around_peak(tokens, "20200703", 3, "balanced") == [
            "20200702",
            "20200703",
            "20200704",
        ]

    def test_invalid_priority_raises(self):
        tokens = self._tokens()
        with pytest.raises(ValueError):
            subsample_around_peak(tokens, "20200703", 3, "weird")

    def test_peak_token_not_in_list_raises(self):
        tokens = self._tokens()
        with pytest.raises(ValueError):
            subsample_around_peak(tokens, "19990101", 2, "post")
