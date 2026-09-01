"""Shared peak-flood date selection helpers for the GFM/MODIS/VIIRS fetchers.

Date-token parsing, peak-window filtering and subsampling are identical
across all three fetchers, so they live here once. Each fetcher's own
``selection.py`` keeps its source-specific flood signal (``flood_pixel_count``,
and MODIS's ``cloud_aware_score``) and re-exports the shared helpers below.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable, TypeVar

T = TypeVar("T")


def parse_yyyymmdd(token: str) -> date | None:
    """Parse an 8-digit YYYYMMDD date token. Returns None for non-date tokens (e.g. 'aggregated')."""
    if len(token) == 8 and token.isdigit():
        return date(int(token[:4]), int(token[4:6]), int(token[6:8]))
    return None


def is_better_peak_candidate(count: int, best_count: int) -> bool:
    """True when *count* should replace the current best (strictly greater)."""
    return count > best_count


def select_peak_window(
    date_tokens: list[str],
    processed_map: dict[str, T],
    *,
    count_fn: Callable[[T], int],
    days_before: int = 0,
    days_after: int = 0,
) -> list[str]:
    """Return the subset of *date_tokens* falling within a window around the peak.

    The peak is the date with the maximum *count_fn* score. Backs each fetcher's
    ``select_peak_window()`` (GFM/MODIS/VIIRS), which supplies its own
    ``count_fn`` (typically that module's ``flood_pixel_count``).

    Args:
        date_tokens: Ordered list of YYYYMMDD date-token strings to filter.
        processed_map: Mapping from date token to a processed-tile object.
        count_fn: Returns a comparable flood signal for a processed tile.
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

    parseable = [(t, parse_yyyymmdd(t)) for t in date_tokens]
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
        count = count_fn(tile)
        if count > best_count:
            best_count = count
            best_token = token

    if best_token is None:
        return [t for t, _ in dated]

    peak_date = parse_yyyymmdd(best_token)
    assert peak_date is not None
    window_start = peak_date - timedelta(days=days_before)
    window_end = peak_date + timedelta(days=days_after)

    return [t for t, d in dated if window_start <= d <= window_end]


def subsample_around_peak(
    date_tokens: list[str],
    peak_token: str,
    max_observations: int,
    priority: str = "post",
) -> list[str]:
    """Return at most *max_observations* tokens, always keeping the peak.

    Selection order depends on *priority*:

    - ``"post"``     — peak first, then chronological post-peak days, then pre-peak
      days closest to peak.
    - ``"pre"``      — peak first, then reverse-chronological pre-peak days, then
      post-peak days closest to peak.
    - ``"balanced"`` — peak first, then alternating ±1, ±2, … offsets.

    The returned list is always sorted in chronological order.

    Args:
        date_tokens: Ordered (chronological) list of candidate tokens.
        peak_token: The token that must be included first.
        max_observations: Maximum number of tokens to return. 0 or negative means
            return all tokens unchanged.
        priority: Subsampling bias: ``"post"``, ``"pre"``, or ``"balanced"``.

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
