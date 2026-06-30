"""Shared helpers for building georeferenced datasets from processed tiles.

Consolidates the per-fetcher ``_as_georeferenced_da`` / ``processed_tile_to_dataset``
logic that was previously duplicated across the GFM, VIIRS, and MODIS fetchers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import xarray as xr
    from rasterio.transform import Affine


def georeference_array(
    array: np.ndarray,
    transform: "Affine",
    crs: str,
    *,
    name: str,
    dtype: np.dtype,
) -> "xr.DataArray":
    """Wrap a raw 2D array in a georeferenced :class:`xarray.DataArray`.

    Attaches pixel-centre coordinates plus CRS / transform so consumers
    (plotting, slicing, harmoniser fallbacks) see a fully georeferenced array.

    Args:
        array: Source array; squeezed to 2D.
        transform: Affine transform for the output grid.
        crs: Coordinate reference system string (e.g. ``"EPSG:4326"``).
        name: DataArray name.
        dtype: Target dtype for the array.

    Returns:
        Georeferenced DataArray with ``(y, x)`` dims.
    """
    import rioxarray  # noqa: F401 — registers .rio on xarray objects
    import xarray as xr

    data = np.squeeze(np.asarray(array, dtype=dtype))
    if data.ndim != 2:
        raise ValueError(f"Expected 2D array for '{name}', got shape {array.shape} after squeeze")
    height, width = data.shape
    x_coords = transform.c + (np.arange(width) + 0.5) * transform.a
    y_coords = transform.f + (np.arange(height) + 0.5) * transform.e
    da = xr.DataArray(
        data,
        dims=("y", "x"),
        coords={"y": y_coords, "x": x_coords},
        name=name,
    )
    da.rio.write_crs(crs, inplace=True)
    da.rio.write_transform(transform, inplace=True)
    return da


def build_dataset(
    variables: list[tuple[str, np.ndarray, np.dtype]],
    transform: "Affine",
    crs: str,
    *,
    event_id: str,
    source_id: str,
) -> "xr.Dataset":
    """Assemble a georeferenced :class:`xarray.Dataset` from named arrays.

    Args:
        variables: Ordered ``(name, array, dtype)`` triples to include.
        transform: Affine transform for the output grid.
        crs: Coordinate reference system string.
        event_id: Flood event identifier (stored as attr).
        source_id: Data source identifier (stored as attr).

    Returns:
        Dataset with one variable per triple and ``source_id`` / ``event_id`` attrs.
    """
    import xarray as xr

    data_vars = {
        name: georeference_array(array, transform, crs, name=name, dtype=dtype) for name, array, dtype in variables
    }
    dataset = xr.Dataset(data_vars)
    dataset.attrs["source_id"] = source_id
    dataset.attrs["event_id"] = event_id
    return dataset
