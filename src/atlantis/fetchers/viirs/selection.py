"""Peak-flood date selection for multi-date VIIRS fetches."""

from __future__ import annotations

from atlantis.fetchers._selection import is_better_peak_candidate as is_better_peak_candidate
from atlantis.fetchers._selection import parse_yyyymmdd
from atlantis.fetchers._selection import select_peak_window as _select_peak_window
from atlantis.fetchers._selection import subsample_around_peak as subsample_around_peak
from atlantis.fetchers.viirs.processor import ProcessedTile

_parse_yyyymmdd = parse_yyyymmdd


def flood_pixel_count(processed: ProcessedTile) -> int:
    """Return a comparable flood signal for picking the peak inundation date."""
    if processed.flood_fraction is not None:
        return int((processed.flood_fraction > 0).sum())
    if processed.raw is not None:
        values = processed.raw.ravel()
        return int(((values >= 101) & (values <= 200)).sum())
    return 0


# ── Peak-window filter ────────────────────────────────────────────────────────


def select_peak_window(
    date_tokens: list[str],
    processed_map: dict[str, ProcessedTile],
    *,
    days_before: int = 0,
    days_after: int = 0,
) -> list[str]:
    """Return the subset of *date_tokens* that fall within a window around the peak date.

    The peak is the date with the maximum flood-pixel count (ties broken by the
    earliest date, consistent with the ``peak`` strategy in :class:`VIIRSFetcher`).

    Args:
        date_tokens: Ordered list of YYYYMMDD date-token strings to filter.
        processed_map: Mapping from date token to :class:`ProcessedTile`.
        days_before: How many days before the peak to include (inclusive).
        days_after: How many days after the peak to include (inclusive).

    Returns:
        Ordered subset of *date_tokens* within the window.  If *days_before* and
        *days_after* are both 0, the full list is returned unchanged.  Non-parseable
        tokens (e.g. ``"aggregated"``) are always excluded.

    Raises:
        ValueError: If *days_before* or *days_after* is negative.
    """
    return _select_peak_window(
        date_tokens,
        processed_map,
        count_fn=flood_pixel_count,
        days_before=days_before,
        days_after=days_after,
    )
