"""Build georeferenced xarray datasets from in-memory MCDWD tiles.

Mirrors :mod:`atlantis.fetchers.viirs.dataset` with the addition of the
MCDWD-only ``recurring_flood`` layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from atlantis.fetchers._dataset import build_dataset
from atlantis.fetchers.modis.processor import ProcessedTile

if TYPE_CHECKING:
    import xarray as xr


def processed_tile_to_dataset(
    processed: ProcessedTile,
    *,
    event_id: str,
    source_id: str,
) -> "xr.Dataset":
    """Convert a :class:`ProcessedTile` to an rioxarray-backed Dataset."""
    if processed.is_classified:
        variables = [
            ("flood_fraction", processed.flood_fraction, np.float32),
            ("quality_mask", processed.quality_mask, np.uint8),
            ("permanent_water", processed.permanent_water, np.uint8),
        ]
        if processed.recurring_flood is not None:
            variables.append(("recurring_flood", processed.recurring_flood, np.uint8))
    else:
        variables = [("raw", processed.raw, processed.raw.dtype)]
    return build_dataset(
        variables,
        processed.transform,
        processed.crs,
        event_id=event_id,
        source_id=source_id,
    )
