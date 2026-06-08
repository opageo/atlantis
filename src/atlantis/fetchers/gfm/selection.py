"""Peak-flood date selection for multi-date GFM fetches.

GFM encoding:
    ``flood_fraction``: float32 in [0, 1] — fraction of observations with flood.
    NaN marks pixels with no valid observation (cloud/nodata).

The peak strategy picks the date with the highest flood pixel count,
analogous to ``atlantis.fetchers.viirs.selection`` and
``atlantis.fetchers.modis.selection``.

Two additional filters (parity with VIIRS / MODIS):

- :func:`select_peak_window` — keep only dates within a ±N-day window
  around the peak-flood date.
- :func:`subsample_around_peak` — cap the result count to *max_observations*,
  biased toward post/pre/balanced offsets from the peak.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from atlantis.fetchers.gfm.processor import GfmProcessedTile


def flood_pixel_count(processed: GfmProcessedTile) -> int:
    """Return a comparable flood signal for picking the peak inundation date.

    Counts pixels where ``flood_fraction > 0``, ignoring NaN (unobserved).
    """
    ff = processed.flood_fraction
    return int(np.nansum(ff > 0))


def is_better_peak_candidate(count: int, best_count: int) -> bool:
    """True when *count* should replace the current best (strictly greater)."""
    return count > best_count


# ── Helpers ──────────────────────────────────────────────────────────────────


def _parse_yyyymmdd(token: str) -> date | None:
    """Parse an 8-digit YYYYMMDD date token. Returns None for non-date tokens."""
    if len(token) == 8 and token.isdigit():
        return date(int(token[:4]), int(token[4:6]), int(token[6:8]))
    return None


# ── Peak-window filter ───────────────────────────────────────────────────────


def select_peak_window(
    date_tokens: list[str],
    processed_map: dict[str, GfmProcessedTile],
    *,
    days_before: int = 0,
    days_after: int = 0,
) -> list[str]:
    """Return the subset of *date_tokens* falling within a window around the peak.

    The peak is the date with the maximum :func:`flood_pixel_count`. Mirrors
    :func:`atlantis.fetchers.viirs.selection.select_peak_window`.

    Args:
        date_tokens: Ordered list of YYYYMMDD date-token strings to filter.
        processed_map: Mapping from date token to :class:`GfmProcessedTile`.
        days_before: How many days before the peak to include (inclusive).
        days_after: How many days after the peak to include (inclusive).

    Returns:
        Ordered subset of *date_tokens* within the window. If both window
        bounds are 0, the full list is returned unchanged. Non-parseable
        tokens are excluded.

    Raises:
        ValueError: If *days_before* or *days_after* is negative.
    """
    if days_before < 0:
        raise ValueError(f"days_before must be non-negative, got {days_before}")
    if days_after < 0:
        raise ValueError(f"days_after must be non-negative, got {days_after}")

    parseable = [(t, _parse_yyyymmdd(t)) for t in date_tokens]
    dated = [(t, d) for t, d in parseable if d is not None]

    if not dated:
        return []

    if days_before == 0 and days_after == 0:
        return [t for t, _ in dated]

    best_token: str | None = None
    best_count = -1
    for token, _ in dated:
        tile = processed_map.get(token)
        if tile is None:
            continue
        count = flood_pixel_count(tile)
        if count > best_count:
            best_count = count
            best_token = token

    if best_token is None:
        return [t for t, _ in dated]

    peak_date = _parse_yyyymmdd(best_token)
    assert peak_date is not None
    window_start = peak_date - timedelta(days=days_before)
    window_end = peak_date + timedelta(days=days_after)

    return [t for t, d in dated if window_start <= d <= window_end]


# ── Subsampler ───────────────────────────────────────────────────────────────


def subsample_around_peak(
    date_tokens: list[str],
    peak_token: str,
    max_observations: int,
    priority: str = "post",
) -> list[str]:
    """Return at most *max_observations* tokens, always keeping the peak.

    Mirrors :func:`atlantis.fetchers.viirs.selection.subsample_around_peak`.

    Args:
        date_tokens: Ordered (chronological) list of candidate tokens.
        peak_token: The token that must be included first.
        max_observations: Maximum number of tokens to return. 0 or negative means
            return all tokens unchanged.
        priority: ``"post"``, ``"pre"``, or ``"balanced"``.

    Returns:
        Chronologically ordered subset of *date_tokens*.

    Raises:
        ValueError: If *peak_token* is not in *date_tokens*, or *priority* is invalid.
    """
    valid_priorities = {"post", "pre", "balanced"}
    if priority not in valid_priorities:
        raise ValueError(f"Invalid priority '{priority}'. Expected one of: {', '.join(sorted(valid_priorities))}")

    if peak_token not in date_tokens:
        raise ValueError(f"peak_token '{peak_token}' not found in date_tokens")

    if max_observations <= 0 or max_observations >= len(date_tokens):
        return list(date_tokens)

    peak_idx = date_tokens.index(peak_token)
    pre = list(reversed(date_tokens[:peak_idx]))
    post = list(date_tokens[peak_idx + 1 :])

    selected: list[str] = [peak_token]
    budget = max_observations - 1

    if priority == "post":
        for token in post:
            if budget <= 0:
                break
            selected.append(token)
            budget -= 1
        for token in pre:
            if budget <= 0:
                break
            selected.append(token)
            budget -= 1
    elif priority == "pre":
        for token in pre:
            if budget <= 0:
                break
            selected.append(token)
            budget -= 1
        for token in post:
            if budget <= 0:
                break
            selected.append(token)
            budget -= 1
    else:  # balanced
        max_len = max(len(pre), len(post))
        for i in range(max_len):
            if budget <= 0:
                break
            if i < len(post):
                selected.append(post[i])
                budget -= 1
            if budget <= 0:
                break
            if i < len(pre):
                selected.append(pre[i])
                budget -= 1

    token_order = {t: idx for idx, t in enumerate(date_tokens)}
    return sorted(selected, key=lambda t: token_order[t])
