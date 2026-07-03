"""Build georeferenced xarray datasets from in-memory MCDWD tiles.

Mirrors :mod:`atlantis.fetchers.viirs.dataset` with the addition of the
MCDWD-only ``recurring_flood`` layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from atlantis.fetchers._dataset import dataset_from_processed
from atlantis.fetchers.modis.layers import registry
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
    return dataset_from_processed(processed, registry, event_id=event_id, source_id=source_id)
