"""Build georeferenced xarray datasets from in-memory MCDWD tiles.

Mirrors :mod:`atlantis.fetchers.viirs.dataset` with the addition of the
MCDWD-only ``recurring_flood`` layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

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
    import rioxarray  # noqa: F401 — registers .rio on xarray objects
    import xarray as xr

    variables: dict[str, xr.DataArray] = {}
    if processed.raw is not None:
        variables["raw"] = _as_georeferenced_da(processed.raw, processed, name="raw", dtype=processed.raw.dtype)
    else:
        if processed.flood_fraction is not None:
            variables["flood_fraction"] = _as_georeferenced_da(
                processed.flood_fraction,
                processed,
                name="flood_fraction",
                dtype=np.float32,
            )
        if processed.quality_mask is not None:
            variables["quality_mask"] = _as_georeferenced_da(
                processed.quality_mask,
                processed,
                name="quality_mask",
                dtype=np.uint8,
            )
        if processed.permanent_water is not None:
            variables["permanent_water"] = _as_georeferenced_da(
                processed.permanent_water,
                processed,
                name="permanent_water",
                dtype=np.uint8,
            )
        if processed.recurring_flood is not None:
            variables["recurring_flood"] = _as_georeferenced_da(
                processed.recurring_flood,
                processed,
                name="recurring_flood",
                dtype=np.uint8,
            )

    dataset = xr.Dataset(variables)
    dataset.attrs["source_id"] = source_id
    dataset.attrs["event_id"] = event_id
    return dataset


def _as_georeferenced_da(
    array: np.ndarray,
    processed: ProcessedTile,
    *,
    name: str,
    dtype: np.dtype,
) -> "xr.DataArray":
    import xarray as xr

    data = np.asarray(array, dtype=dtype)
    height, width = data.shape
    transform = processed.transform
    x_coords = transform.c + (np.arange(width) + 0.5) * transform.a
    y_coords = transform.f + (np.arange(height) + 0.5) * transform.e
    da = xr.DataArray(
        data,
        dims=("y", "x"),
        coords={"y": y_coords, "x": x_coords},
        name=name,
    )
    da.rio.write_crs(processed.crs, inplace=True)
    da.rio.write_transform(transform, inplace=True)
    return da
