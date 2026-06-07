"""Build georeferenced xarray datasets from GFM processed tiles."""

from __future__ import annotations

import numpy as np
import rioxarray  # noqa: F401
import xarray as xr

from atlantis.fetchers.gfm.processor import GfmProcessedTile


def processed_tile_to_dataset(
    processed: GfmProcessedTile,
    *,
    event_id: str,
    source_id: str = "gfm",
) -> "xr.Dataset":
    """Convert a GfmProcessedTile to an rioxarray-backed Dataset.

    Args:
        processed: The processed tile with flood_fraction, quality_mask,
            and permanent_water arrays.
        event_id: Flood event identifier.
        source_id: Data source identifier.

    Returns:
        xarray Dataset with georeferenced flood variables.
    """
    variables: dict[str, xr.DataArray] = {}

    variables["flood_fraction"] = _as_georeferenced_da(
        processed.flood_fraction,
        processed,
        name="flood_fraction",
        dtype=np.float32,
    )
    variables["quality_mask"] = _as_georeferenced_da(
        processed.quality_mask,
        processed,
        name="quality_mask",
        dtype=np.uint8,
    )
    variables["permanent_water"] = _as_georeferenced_da(
        processed.permanent_water,
        processed,
        name="permanent_water",
        dtype=np.uint8,
    )

    dataset = xr.Dataset(variables)
    dataset.attrs["source_id"] = source_id
    dataset.attrs["event_id"] = event_id
    return dataset


def _as_georeferenced_da(
    array: np.ndarray,
    processed: GfmProcessedTile,
    *,
    name: str,
    dtype: np.dtype,
) -> "xr.DataArray":
    """Create a georeferenced DataArray from raw array data."""
    data = np.squeeze(np.asarray(array, dtype=dtype))
    if data.ndim != 2:
        raise ValueError(f"Expected 2D array for '{name}', got shape {array.shape} after squeeze")
    height, width = data.shape
    transform = processed.transform
    x_coords = transform.c + (np.arange(width) + 0.5) * transform.a
    y_coords = transform.f + (np.arange(height) + 0.5) * transform.e
    da = xr.DataArray(
        data,
        dims=("y", "x"),
        coords={"x": x_coords, "y": y_coords},
        name=name,
    )
    da.rio.write_crs(processed.crs, inplace=True)
    da.rio.write_transform(transform, inplace=True)
    return da
