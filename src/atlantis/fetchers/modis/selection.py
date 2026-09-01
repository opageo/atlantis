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

Date-token parsing, peak-window filtering and subsampling are shared with the
GFM/VIIRS selectors via :mod:`atlantis.fetchers._selection`.
"""

from __future__ import annotations

from atlantis.fetchers._selection import is_better_peak_candidate as is_better_peak_candidate
from atlantis.fetchers._selection import parse_yyyymmdd
from atlantis.fetchers._selection import select_peak_window as _select_peak_window
from atlantis.fetchers._selection import subsample_around_peak as subsample_around_peak
from atlantis.fetchers.modis.processor import (
    INSUFFICIENT_DATA_CODE,
    RECURRING_FLOOD_CODE,
    UNUSUAL_FLOOD_CODE,
    ProcessedTile,
)

_parse_yyyymmdd = parse_yyyymmdd


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
    return _select_peak_window(
        date_tokens,
        processed_map,
        count_fn=lambda tile: flood_pixel_count(tile, include_recurring=include_recurring),
        days_before=days_before,
        days_after=days_after,
    )
