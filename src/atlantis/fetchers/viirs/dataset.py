"""Build georeferenced xarray datasets from in-memory VIIRS tiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

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
) -> xr.DataArray:
    import xarray as xr

    data = np.asarray(array, dtype=dtype)
    da = xr.DataArray(data, dims=("y", "x"), name=name)
    da.rio.write_crs(processed.crs, inplace=True)
    da.rio.write_transform(processed.transform, inplace=True)
    return da


def processed_tiles_to_multi_dataset(
    tiles: list[tuple[str, ProcessedTile]],
    event_id: str,
    source_id: str = "viirs",
) -> xr.Dataset:
    """Convert multiple ProcessedTiles to a multi-date xarray Dataset."""
    import xarray as xr

    datasets = []
    for date_token, result in tiles:
        ds = processed_tile_to_dataset(result.processed, event_id, source_id)
        ds = ds.expand_dims(time=[date_token])
        datasets.append(ds)

    return xr.concat(datasets, dim="time")
