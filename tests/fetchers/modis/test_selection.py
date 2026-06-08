"""Tests for MODIS peak-date selection helpers."""

from __future__ import annotations

import numpy as np
import pytest
from rasterio.transform import from_origin

from atlantis.fetchers.modis.processor import ProcessedTile
from atlantis.fetchers.modis.selection import (
    cloud_aware_score,
    flood_pixel_count,
    is_better_peak_candidate,
)


def _processed(
    *,
    flood: np.ndarray | None = None,
    raw: np.ndarray | None = None,
    quality: np.ndarray | None = None,
    recurring: np.ndarray | None = None,
) -> ProcessedTile:
    transform = from_origin(0.0, 1.0, 1.0, -1.0)
    return ProcessedTile(
        transform=transform,
        crs="EPSG:4326",
        cloud_fraction=0.0,
        raw=raw,
        flood_fraction=flood,
        quality_mask=quality,
        recurring_flood=recurring,
    )


class TestFloodPixelCount:
    def test_classified_counts_positive_flood_fraction(self):
        flood = np.array([[0.0, 1.0], [1.0, 0.0]], dtype=np.float32)
        assert flood_pixel_count(_processed(flood=flood)) == 2

    def test_raw_counts_only_unusual_flood_by_default(self):
        raw = np.array([[0, 1], [2, 3]], dtype=np.uint8)  # one each of {0,1,2,3}
        assert flood_pixel_count(_processed(raw=raw)) == 1

    def test_raw_with_include_recurring(self):
        raw = np.array([[0, 1], [2, 3]], dtype=np.uint8)
        assert flood_pixel_count(_processed(raw=raw), include_recurring=True) == 2

    def test_classified_with_include_recurring(self):
        flood = np.array([[0.0, 1.0], [0.0, 0.0]], dtype=np.float32)
        recurring = np.array([[0, 0], [1, 0]], dtype=np.uint8)
        assert flood_pixel_count(_processed(flood=flood, recurring=recurring), include_recurring=True) == 2

    def test_empty_returns_zero(self):
        assert flood_pixel_count(_processed()) == 0


class TestIsBetterPeakCandidate:
    def test_strictly_greater(self):
        assert is_better_peak_candidate(5, 4) is True
        assert is_better_peak_candidate(4, 4) is False


class TestCloudAwareScore:
    def test_score_uses_raw_when_present(self):
        # 50% flood (class 3), no missing pixels → score = 1.0 * 0.5 = 0.5
        raw = np.array([[3, 3], [0, 0]], dtype=np.uint8)
        score = cloud_aware_score(_processed(raw=raw))
        assert score == pytest.approx(0.5, rel=1e-6)

    def test_score_penalises_missing(self):
        # Total = 4, missing = 1, valid = 3, flood = 1.
        # score = (flood/valid) * (1 - missing/total) = (1/3) * 0.75 = 0.25
        raw = np.array([[3, 0], [0, 255]], dtype=np.uint8)
        score = cloud_aware_score(_processed(raw=raw))
        assert score == pytest.approx(1 / 3 * 0.75, rel=1e-6)

    def test_min_valid_fraction_filters_cloudy_dates(self):
        # 99% missing → eligibility floor not met
        raw = np.full((10, 10), 255, dtype=np.uint8)
        raw[0, 0] = 3
        score = cloud_aware_score(_processed(raw=raw), min_valid_fraction=0.05)
        assert score == float("-inf")

    def test_include_recurring_lifts_score(self):
        raw = np.array([[2, 2], [0, 0]], dtype=np.uint8)
        # Default (class 3 only): no flood → score 0.
        assert cloud_aware_score(_processed(raw=raw)) == 0.0
        # With recurring folded in: 50% flood-like → 0.5.
        assert cloud_aware_score(_processed(raw=raw), include_recurring=True) == pytest.approx(0.5, rel=1e-6)

    def test_score_uses_classified_when_raw_absent(self):
        # Quality mask shows 1 missing pixel out of 4; 1 flood → score = 0.5 * 0.75 = 0.375
        flood = np.array([[1.0, 0.0], [0.0, 0.0]], dtype=np.float32)
        quality = np.array([[1, 1], [1, 0]], dtype=np.uint8)
        score = cloud_aware_score(_processed(flood=flood, quality=quality))
        assert score == pytest.approx(1 / 3 * 0.75, rel=1e-6)


# ── select_peak_window / subsample_around_peak (parity with VIIRS) ──────────


def _make_flood_tile(n_flood_pixels: int) -> ProcessedTile:
    """Synthetic ProcessedTile with *n_flood_pixels* positive flood_fraction cells."""
    data = np.zeros((4, 4), dtype=np.float32)
    data.ravel()[:n_flood_pixels] = 0.7
    return _processed(flood=data)


class TestSelectPeakWindow:
    def _five_date_map(self):
        """5 dates spanning 2020-07-01..05, peak on day 3 (most flood pixels)."""
        from atlantis.fetchers.modis.selection import select_peak_window  # noqa: F401

        tokens = ["20200701", "20200702", "20200703", "20200704", "20200705"]
        counts = [2, 5, 16, 8, 3]
        return tokens, {t: _make_flood_tile(c) for t, c in zip(tokens, counts)}

    def test_zero_window_returns_all(self):
        from atlantis.fetchers.modis.selection import select_peak_window

        tokens, pmap = self._five_date_map()
        assert select_peak_window(tokens, pmap, days_before=0, days_after=0) == tokens

    def test_symmetric_window_filters_correctly(self):
        from atlantis.fetchers.modis.selection import select_peak_window

        tokens, pmap = self._five_date_map()
        result = select_peak_window(tokens, pmap, days_before=1, days_after=1)
        assert result == ["20200702", "20200703", "20200704"]

    def test_negative_days_before_raises(self):
        from atlantis.fetchers.modis.selection import select_peak_window

        tokens, pmap = self._five_date_map()
        with pytest.raises(ValueError):
            select_peak_window(tokens, pmap, days_before=-1, days_after=0)

    def test_non_date_tokens_excluded(self):
        from atlantis.fetchers.modis.selection import select_peak_window

        tokens, pmap = self._five_date_map()
        tokens = ["aggregated"] + tokens
        pmap["aggregated"] = _make_flood_tile(100)  # would be peak but should be excluded
        result = select_peak_window(tokens, pmap, days_before=1, days_after=1)
        assert "aggregated" not in result
        assert result == ["20200702", "20200703", "20200704"]


class TestSubsampleAroundPeak:
    def _tokens(self):
        return ["20200701", "20200702", "20200703", "20200704", "20200705"]

    def test_no_limit_returns_all(self):
        from atlantis.fetchers.modis.selection import subsample_around_peak

        tokens = self._tokens()
        assert subsample_around_peak(tokens, "20200703", max_observations=0) == tokens

    def test_max_one_returns_only_peak(self):
        from atlantis.fetchers.modis.selection import subsample_around_peak

        tokens = self._tokens()
        assert subsample_around_peak(tokens, "20200703", max_observations=1) == ["20200703"]

    def test_post_priority(self):
        from atlantis.fetchers.modis.selection import subsample_around_peak

        tokens = self._tokens()
        # peak + 2 post = days 3, 4, 5
        assert subsample_around_peak(tokens, "20200703", 3, "post") == ["20200703", "20200704", "20200705"]

    def test_pre_priority(self):
        from atlantis.fetchers.modis.selection import subsample_around_peak

        tokens = self._tokens()
        # peak + 2 pre = days 1, 2, 3
        assert subsample_around_peak(tokens, "20200703", 3, "pre") == ["20200701", "20200702", "20200703"]

    def test_balanced_priority(self):
        from atlantis.fetchers.modis.selection import subsample_around_peak

        tokens = self._tokens()
        # peak + 1 post + 1 pre = days 2, 3, 4
        assert subsample_around_peak(tokens, "20200703", 3, "balanced") == ["20200702", "20200703", "20200704"]

    def test_invalid_priority_raises(self):
        from atlantis.fetchers.modis.selection import subsample_around_peak

        tokens = self._tokens()
        with pytest.raises(ValueError):
            subsample_around_peak(tokens, "20200703", 3, "weird")

    def test_peak_token_not_in_list_raises(self):
        from atlantis.fetchers.modis.selection import subsample_around_peak

        tokens = self._tokens()
        with pytest.raises(ValueError):
            subsample_around_peak(tokens, "19990101", 2, "post")
