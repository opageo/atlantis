"""Build georeferenced xarray datasets from GFM processed tiles."""

from __future__ import annotations

import numpy as np
import xarray as xr

from atlantis.fetchers._dataset import build_dataset
from atlantis.fetchers.gfm.processor import GfmProcessedTile


def processed_tile_to_dataset(
    processed: GfmProcessedTile,
    *,
    event_id: str,
    source_id: str = "gfm",
) -> "xr.Dataset":
    """Convert a GfmProcessedTile to an rioxarray-backed Dataset.

    Classified mode emits ``flood_fraction``, ``quality_mask``, and
    ``permanent_water``.  Native / raw mode emits the two native bands
    ``ensemble_flood_extent`` and ``reference_water_mask``.

    Args:
        processed: The processed tile from :class:`GfmRasterProcessor`.
        event_id: Flood event identifier.
        source_id: Data source identifier.

    Returns:
        xarray Dataset with georeferenced flood variables.
    """
    if processed.is_classified:
        variables = [
            ("flood_fraction", processed.flood_fraction, np.float32),
            ("quality_mask", processed.quality_mask, np.uint8),
            ("permanent_water", processed.permanent_water, np.uint8),
        ]
    else:
        variables = [
            ("ensemble_flood_extent", processed.ensemble_flood_extent, np.uint8),
            ("reference_water_mask", processed.reference_water_mask, np.uint8),
        ]
    return build_dataset(
        variables,
        processed.transform,
        processed.crs,
        event_id=event_id,
        source_id=source_id,
    )
