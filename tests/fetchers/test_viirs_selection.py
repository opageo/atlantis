"""Tests for VIIRS peak-date selection helpers."""

import numpy as np
from rasterio.transform import from_origin

from atlantis.fetchers.viirs.processor import ProcessedTile
from atlantis.fetchers.viirs.selection import (
    _parse_yyyymmdd,
    flood_pixel_count,
    is_better_peak_candidate,
    select_peak_window,
    subsample_around_peak,
)


def _processed(*, flood: np.ndarray | None = None, raw: np.ndarray | None = None) -> ProcessedTile:
    transform = from_origin(0.0, 1.0, 1.0, -1.0)
    return ProcessedTile(
        transform=transform,
        crs="EPSG:4326",
        cloud_fraction=0.0,
        raw=raw,
        flood_fraction=flood,
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


# ── _parse_yyyymmdd ───────────────────────────────────────────────────────────


class TestParseYyyymmdd:
    def test_valid_token(self):
        from datetime import date

        assert _parse_yyyymmdd("20200722") == date(2020, 7, 22)

    def test_aggregated_token_returns_none(self):
        assert _parse_yyyymmdd("aggregated") is None

    def test_short_token_returns_none(self):
        assert _parse_yyyymmdd("2020072") is None

    def test_long_token_returns_none(self):
        assert _parse_yyyymmdd("202007220") is None


# ── select_peak_window ────────────────────────────────────────────────────────


def _make_flood_tile(n_flood_pixels: int) -> ProcessedTile:
    """Create a synthetic ProcessedTile with *n_flood_pixels* flooded."""
    data = np.zeros((4, 4), dtype=np.float32)
    data.ravel()[:n_flood_pixels] = 0.7
    return _processed(flood=data)


class TestSelectPeakWindow:
    def _five_date_map(self):
        """5 dates: day 1–5, peak on day 3 (most flood pixels)."""
        tokens = ["20200701", "20200702", "20200703", "20200704", "20200705"]
        counts = [2, 5, 16, 8, 3]
        return tokens, {t: _make_flood_tile(c) for t, c in zip(tokens, counts)}

    def test_zero_window_returns_all(self):
        tokens, pmap = self._five_date_map()
        result = select_peak_window(tokens, pmap, days_before=0, days_after=0)
        assert result == tokens

    def test_symmetric_window_filters_correctly(self):
        tokens, pmap = self._five_date_map()
        # Peak = day 3 (index 2). ±1 day → days 2,3,4
        result = select_peak_window(tokens, pmap, days_before=1, days_after=1)
        assert result == ["20200702", "20200703", "20200704"]

    def test_asymmetric_window_before_only(self):
        tokens, pmap = self._five_date_map()
        # Peak = day 3. 2 days before, 0 after → days 1,2,3
        result = select_peak_window(tokens, pmap, days_before=2, days_after=0)
        assert result == ["20200701", "20200702", "20200703"]

    def test_asymmetric_window_after_only(self):
        tokens, pmap = self._five_date_map()
        # Peak = day 3. 0 before, 2 after → days 3,4,5
        result = select_peak_window(tokens, pmap, days_before=0, days_after=2)
        assert result == ["20200703", "20200704", "20200705"]

    def test_window_larger_than_range_returns_all(self):
        tokens, pmap = self._five_date_map()
        result = select_peak_window(tokens, pmap, days_before=30, days_after=30)
        assert result == tokens

    def test_window_clips_to_available_dates(self):
        tokens, pmap = self._five_date_map()
        # Peak = day 3. 5 days before → would reach pre-day-1 range; clipped
        result = select_peak_window(tokens, pmap, days_before=5, days_after=0)
        # All dates from day 1 to peak (days 1-3) are within window
        assert "20200701" in result
        assert "20200703" in result

    def test_empty_token_list_returns_empty(self):
        assert select_peak_window([], {}, days_before=1, days_after=1) == []

    def test_non_date_tokens_excluded(self):
        tokens = ["aggregated"]
        result = select_peak_window(tokens, {}, days_before=1, days_after=1)
        assert result == []

    def test_tie_breaking_earliest_wins(self):
        """Tied pixel counts → earliest date is chosen as the peak."""
        tokens = ["20200701", "20200702", "20200703"]
        tie_value = 10
        pmap = {t: _make_flood_tile(tie_value) for t in tokens}
        # Window [0, +1] around peak (day 1) → days 1,2
        result = select_peak_window(tokens, pmap, days_before=0, days_after=1)
        assert result == ["20200701", "20200702"]

    def test_negative_days_before_raises(self):
        import pytest

        tokens, pmap = self._five_date_map()
        with pytest.raises(ValueError, match="days_before"):
            select_peak_window(tokens, pmap, days_before=-1, days_after=0)

    def test_negative_days_after_raises(self):
        import pytest

        tokens, pmap = self._five_date_map()
        with pytest.raises(ValueError, match="days_after"):
            select_peak_window(tokens, pmap, days_before=0, days_after=-1)


# ── subsample_around_peak ─────────────────────────────────────────────────────


class TestSubsampleAroundPeak:
    def _seven_tokens(self):
        return ["20200701", "20200702", "20200703", "20200704", "20200705", "20200706", "20200707"]

    def _peak(self):
        return "20200704"  # day 4 of 7

    def test_no_limit_returns_all(self):
        tokens = self._seven_tokens()
        result = subsample_around_peak(tokens, self._peak(), max_observations=0)
        assert result == tokens

    def test_max_larger_than_window_returns_all(self):
        tokens = self._seven_tokens()
        result = subsample_around_peak(tokens, self._peak(), max_observations=100)
        assert result == tokens

    def test_max_one_returns_only_peak(self):
        tokens = self._seven_tokens()
        result = subsample_around_peak(tokens, self._peak(), max_observations=1)
        assert result == [self._peak()]

    def test_post_priority_prefers_post_dates(self):
        tokens = self._seven_tokens()
        # Peak = day 4. Budget 3 → peak + days 5, 6
        result = subsample_around_peak(tokens, self._peak(), max_observations=3, priority="post")
        assert self._peak() in result
        assert "20200705" in result
        assert "20200706" in result
        assert len(result) == 3
        assert result == sorted(result)  # chronological order

    def test_pre_priority_prefers_pre_dates(self):
        tokens = self._seven_tokens()
        # Peak = day 4. Budget 3 → peak + days 3, 2
        result = subsample_around_peak(tokens, self._peak(), max_observations=3, priority="pre")
        assert self._peak() in result
        assert "20200703" in result
        assert "20200702" in result
        assert len(result) == 3
        assert result == sorted(result)

    def test_balanced_priority_alternates(self):
        tokens = self._seven_tokens()
        # Peak = day 4. Budget 5 → peak + day5, day3, day6, day2
        result = subsample_around_peak(tokens, self._peak(), max_observations=5, priority="balanced")
        assert self._peak() in result
        assert len(result) == 5
        assert result == sorted(result)
        # Day 5 and day 3 (±1) must be included
        assert "20200705" in result
        assert "20200703" in result

    def test_post_exhausts_to_pre(self):
        """If post dates run out, pre dates are used to fill."""
        tokens = ["20200701", "20200702", "20200703", "20200704"]
        peak = "20200703"  # only 1 post date available
        result = subsample_around_peak(tokens, peak, max_observations=3, priority="post")
        assert peak in result
        assert "20200704" in result  # post
        assert "20200702" in result  # fallback to pre
        assert len(result) == 3

    def test_peak_not_in_tokens_raises(self):
        import pytest

        with pytest.raises(ValueError, match="peak_token"):
            subsample_around_peak(["20200701", "20200702"], "20200703", max_observations=2)

    def test_invalid_priority_raises(self):
        import pytest

        with pytest.raises(ValueError, match="priority"):
            subsample_around_peak(["20200701", "20200702"], "20200701", max_observations=2, priority="random")

    def test_result_always_chronological(self):
        tokens = self._seven_tokens()
        for priority in ("post", "pre", "balanced"):
            result = subsample_around_peak(tokens, self._peak(), max_observations=4, priority=priority)
            assert result == sorted(result), f"Not chronological for priority={priority!r}"
