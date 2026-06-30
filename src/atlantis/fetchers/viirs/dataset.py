"""Build georeferenced xarray datasets from in-memory VIIRS tiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from atlantis.fetchers._dataset import build_dataset
from atlantis.fetchers.viirs.processor import ProcessedTile

if TYPE_CHECKING:
    import xarray as xr


def processed_tile_to_dataset(
    processed: ProcessedTile,
    *,
    event_id: str,
    source_id: str,
) -> xr.Dataset:
    """Convert a :class:`ProcessedTile` to an rioxarray-backed Dataset."""
    if processed.is_classified:
        variables = [
            ("flood_fraction", processed.flood_fraction, np.float32),
            ("quality_mask", processed.quality_mask, np.uint8),
            ("permanent_water", processed.permanent_water, np.uint8),
        ]
    else:
        variables = [("raw", processed.raw, processed.raw.dtype)]
    return build_dataset(
        variables,
        processed.transform,
        processed.crs,
        event_id=event_id,
        source_id=source_id,
    )


def processed_tiles_to_multi_dataset(
    tiles: list[tuple[str, ProcessedTile]],
    event_id: str,
    source_id: str = "viirs",
) -> xr.Dataset:
    """Convert multiple ProcessedTiles to a multi-date xarray Dataset."""
    import xarray as xr

    datasets = []
    for date_token, result in tiles:
        ds = processed_tile_to_dataset(result.processed, event_id=event_id, source_id=source_id)
        ds = ds.expand_dims(time=[date_token])
        datasets.append(ds)

    return xr.concat(datasets, dim="time")
