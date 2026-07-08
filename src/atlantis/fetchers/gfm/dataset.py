"""Build georeferenced xarray datasets from GFM processed tiles."""

from __future__ import annotations

import xarray as xr

from atlantis.fetchers._dataset import dataset_from_processed
from atlantis.fetchers.gfm.layers import registry
from atlantis.fetchers.gfm.processor import GfmProcessedTile


def processed_tile_to_dataset(
    processed: GfmProcessedTile,
    *,
    event_id: str,
    source_id: str = "gfm",
) -> "xr.Dataset":
    """Convert a GfmProcessedTile to an rioxarray-backed Dataset.

    Classified mode emits ``water_fraction``, ``flood_fraction``,
    ``reference_water``, and any native-code extras carried on the processed
    tile. Native / raw mode emits the registered native bands such as
    ``ensemble_flood_extent`` and ``reference_water_mask``. The variable set is
    driven by the GFM layer registry.

    Args:
        processed: The processed tile from :class:`GfmRasterProcessor`.
        event_id: Flood event identifier.
        source_id: Data source identifier.

    Returns:
        xarray Dataset with georeferenced flood variables.
    """
    return dataset_from_processed(processed, registry, event_id=event_id, source_id=source_id)
