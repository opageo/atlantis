"""Peak-flood date selection for multi-date GFM fetches.

GFM encoding:
    ``flood_fraction``: float32 in [0, 1] — fraction of observations with flood.
    NaN marks pixels with no valid observation (cloud/nodata).

The peak strategy picks the date with the highest flood pixel count,
analogous to ``atlantis.fetchers.viirs.selection``.
"""

from __future__ import annotations

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
