"""Peak-flood date selection for multi-date VIIRS fetches."""

from __future__ import annotations

from atlantis.fetchers.viirs.processor import ProcessedTile


def flood_pixel_count(processed: ProcessedTile) -> int:
    """Return a comparable flood signal for picking the peak inundation date."""
    if processed.flood_fraction is not None:
        return int((processed.flood_fraction > 0).sum())
    if processed.raw is not None:
        values = processed.raw.ravel()
        return int(((values >= 101) & (values <= 200)).sum())
    return 0


def is_better_peak_candidate(count: int, best_count: int) -> bool:
    """True when *count* should replace the current best (strictly greater)."""
    return count > best_count
