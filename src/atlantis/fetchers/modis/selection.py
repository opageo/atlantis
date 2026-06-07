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
"""

from __future__ import annotations

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
        qm = processed.quality_mask
        total = int(ff.size)
        if qm is not None:
            missing = int((qm == 0).sum())
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
