"""Peak-flood date selection for multi-date GFM fetches.

GFM encoding:
    ``flood_fraction``: float32 in [0, 1] — fraction of observations with flood.
    NaN marks pixels with no valid observation (cloud/nodata).

The peak strategy picks the date with the highest flood pixel count,
analogous to ``atlantis.fetchers.viirs.selection`` and
``atlantis.fetchers.modis.selection``.

Date-token parsing, peak-window filtering and subsampling are shared with the
MODIS/VIIRS selectors via :mod:`atlantis.fetchers._selection`; only the
flood-signal itself (:func:`flood_pixel_count`) is GFM-specific.
"""

from __future__ import annotations

import numpy as np

from atlantis.fetchers._selection import is_better_peak_candidate as is_better_peak_candidate
from atlantis.fetchers._selection import parse_yyyymmdd
from atlantis.fetchers._selection import select_peak_window as _select_peak_window
from atlantis.fetchers._selection import subsample_around_peak as subsample_around_peak
from atlantis.fetchers.gfm.processor import GfmProcessedTile

_parse_yyyymmdd = parse_yyyymmdd


def flood_pixel_count(processed: GfmProcessedTile) -> int:
    """Return a comparable flood signal for picking the peak inundation date.

    Classified mode: counts pixels where ``flood_fraction > 0``, ignoring NaN.
    Native mode: counts pixels where ``ensemble_flood_extent == GFM_FLOOD`` (1).
    """
    from atlantis.fetchers.gfm.processor import GFM_FLOOD, GFM_NODATA

    if processed.flood_fraction is not None:
        ff = processed.flood_fraction
        return int(np.nansum(ff > 0))

    if processed.ensemble_flood_extent is not None:
        efe = processed.ensemble_flood_extent
        return int(np.sum((efe == GFM_FLOOD) & (efe != GFM_NODATA)))

    return 0


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
    return _select_peak_window(
        date_tokens,
        processed_map,
        count_fn=flood_pixel_count,
        days_before=days_before,
        days_after=days_after,
    )
