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

    from atlantis.layers import LayerRegistry


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


def _layer_array(processed: object, name: str) -> "np.ndarray | None":
    """Return the array for layer *name* from a processed tile.

    Looks up a named attribute first (e.g. ``processed.flood_fraction``), then an
    optional ``extra_layers`` dict used to carry layers beyond the core fields.
    """
    array = getattr(processed, name, None)
    if array is None:
        extra = getattr(processed, "extra_layers", None)
        if extra:
            array = extra.get(name)
    return array


def dataset_from_processed(
    processed: object,
    registry: "LayerRegistry",
    *,
    event_id: str,
    source_id: str,
) -> "xr.Dataset":
    """Build a Dataset from a processed tile, driven by the source registry.

    The set of variables is determined by the registry rather than hard-coded:
    classified tiles emit the registry's derived layers; raw/native tiles emit
    the native layers. Only layers actually populated on *processed* are
    included, so optional layers (e.g. MODIS counts) are skipped when absent.

    Args:
        processed: A processed-tile object exposing ``is_classified``,
            ``transform``, ``crs``, and per-layer attributes / ``extra_layers``.
        registry: The source's layer registry.
        event_id: Flood event identifier.
        source_id: Data source identifier.

    Returns:
        Georeferenced dataset with one variable per populated layer.
    """
    specs = registry.list_derived() if processed.is_classified else registry.list_native()
    variables: list[tuple[str, np.ndarray, np.dtype]] = []
    seen: set[str] = set()
    for spec in specs:
        array = _layer_array(processed, spec.name)
        if array is None:
            continue
        variables.append((spec.name, array, np.dtype(spec.dtype)))
        seen.add(spec.name)

    # Carry any extra layers not described by a spec (defensive).
    extra = getattr(processed, "extra_layers", None) or {}
    for name, array in extra.items():
        if array is not None and name not in seen:
            variables.append((name, array, np.asarray(array).dtype))

    return build_dataset(
        variables,
        processed.transform,
        processed.crs,
        event_id=event_id,
        source_id=source_id,
    )
