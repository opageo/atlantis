"""Peak-flood date selection for multi-date MCDWD fetches.

Two selectors:

- :func:`flood_pixel_count` — naive pixel count, used as the default
  "more flood = better" tiebreaker. Mirrors the VIIRS selector.
- :func:`cloud_aware_score` — implements the cloud-aware peak score
  used by ``ifs-floodbench/Scripts/estimate_modis_peak_dates.py``:

    score = flood_fraction_valid × (1 − missing_fraction_total)

  Penalises dates with high cloud cover even if their valid-pixel flood
  fraction is high. Optionally folds class 2 (recurring flood) into the
  numerator for backwards compatibility with pre-Release-1.1 archives.

Two filters (mirroring :mod:`atlantis.fetchers.viirs.selection`):

- :func:`select_peak_window` — keep only dates within a ±N-day window
  around the peak-flood date.
- :func:`subsample_around_peak` — cap the result count to *max_observations*,
  biased toward post/pre/balanced offsets from the peak.
"""

from __future__ import annotations

from datetime import date, timedelta

from atlantis.fetchers.modis.processor import (
    INSUFFICIENT_DATA_CODE,
    RECURRING_FLOOD_CODE,
    UNUSUAL_FLOOD_CODE,
    ProcessedTile,
)


def flood_pixel_count(processed: ProcessedTile, *, include_recurring: bool = False) -> int:
    """Return a comparable flood signal for picking the peak inundation date.

    Args:
        processed: The processed tile.
        include_recurring: When True, also count recurring-flood pixels
            (class 2). Useful for pre-Release-1.1 archives where every
            event-driven flood is emitted as class 3 anyway.
    """
    if processed.flood_fraction is not None:
        count = int((processed.flood_fraction > 0).sum())
        if include_recurring and processed.recurring_flood is not None:
            count += int(processed.recurring_flood.sum())
        return count

    if processed.raw is not None:
        values = processed.raw.ravel()
        if include_recurring:
            return int(((values == UNUSUAL_FLOOD_CODE) | (values == RECURRING_FLOOD_CODE)).sum())
        return int((values == UNUSUAL_FLOOD_CODE).sum())

    return 0


def is_better_peak_candidate(count: int, best_count: int) -> bool:
    """True when *count* should replace the current best (strictly greater)."""
    return count > best_count


def cloud_aware_score(
    processed: ProcessedTile,
    *,
    min_valid_fraction: float = 0.05,
    include_recurring: bool = False,
) -> float:
    """Cloud-aware peak score.

    Returns ``flood_fraction_valid × (1 − missing_fraction_total)``, or
    ``-inf`` when the date does not pass the *min_valid_fraction* filter
    (so it can never win an ``argmax``).

    The implementation matches
    ``ifs-floodbench/Scripts/estimate_modis_peak_dates.py``.
    """
    if processed.raw is not None:
        data = processed.raw
        total = int(data.size)
        missing = int((data == INSUFFICIENT_DATA_CODE).sum())
        valid = total - missing
        flood_codes = data == UNUSUAL_FLOOD_CODE
        if include_recurring:
            flood_codes = flood_codes | (data == RECURRING_FLOOD_CODE)
        flood = int(flood_codes.sum())
    elif processed.flood_fraction is not None:
        ff = processed.flood_fraction
        exclusion = processed.exclusion_mask
        total = int(ff.size)
        if exclusion is not None:
            missing = int((exclusion > 0).sum())
        else:
            missing = 0
        valid = total - missing
        flood_mask = ff > 0
        if include_recurring and processed.recurring_flood is not None:
            flood_mask = flood_mask | (processed.recurring_flood > 0)
        flood = int(flood_mask.sum())
    else:
        return float("-inf")

    if total == 0:
        return float("-inf")

    valid_fraction = valid / total
    if valid_fraction < min_valid_fraction:
        return float("-inf")

    flood_fraction_valid = flood / valid if valid > 0 else 0.0
    missing_fraction_total = missing / total
    return float(flood_fraction_valid * (1.0 - missing_fraction_total))


# ── Helpers ──────────────────────────────────────────────────────────────────


def _parse_yyyymmdd(token: str) -> date | None:
    """Parse an 8-digit YYYYMMDD date token. Returns None for non-date tokens (e.g. 'aggregated')."""
    if len(token) == 8 and token.isdigit():
        return date(int(token[:4]), int(token[4:6]), int(token[6:8]))
    return None


# ── Peak-window filter ───────────────────────────────────────────────────────


def select_peak_window(
    date_tokens: list[str],
    processed_map: dict[str, ProcessedTile],
    *,
    days_before: int = 0,
    days_after: int = 0,
    include_recurring: bool = False,
) -> list[str]:
    """Return the subset of *date_tokens* falling within a window around the peak.

    The peak is the date with the maximum :func:`flood_pixel_count` (ties broken
    by the earliest date, consistent with the ``peak`` strategy in
    :class:`MODISFetcher`).

    Args:
        date_tokens: Ordered list of YYYYMMDD date-token strings to filter.
        processed_map: Mapping from date token to :class:`ProcessedTile`.
        days_before: How many days before the peak to include (inclusive).
        days_after: How many days after the peak to include (inclusive).
        include_recurring: Forwarded to :func:`flood_pixel_count`.

    Returns:
        Ordered subset of *date_tokens* within the window. If *days_before* and
        *days_after* are both 0, the full list is returned unchanged. Non-parseable
        tokens (e.g. ``"aggregated"``) are always excluded.

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
        count = flood_pixel_count(tile, include_recurring=include_recurring)
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
