"""Reprojector for CRS transformation and resampling."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.warp import reproject as rio_reproject

if TYPE_CHECKING:
    import xarray as xr
    from pyproj import CRS


_RESAMPLING_MAP: dict[str, Resampling] = {
    "average": Resampling.average,
    "bilinear": Resampling.bilinear,
    "nearest": Resampling.nearest,
    "cubic": Resampling.cubic,
    "mode": Resampling.mode,
    "max": Resampling.max,
    "min": Resampling.min,
    "med": Resampling.med,
    "q1": Resampling.q1,
    "q3": Resampling.q3,
}


def _rio_available(dataset: xr.Dataset) -> bool:
    """Check if rioxarray's ``.rio`` accessor is available on the dataset."""
    try:
        _ = dataset.rio.crs
        return True
    except Exception:
        return False


def _get_dataset_bounds(dataset: xr.Dataset) -> tuple[float, float, float, float]:
    """Extract bounding box from an xarray Dataset.

    Prefers the rioxarray accessor; falls back to coordinate minima/maxima.
    """
    if _rio_available(dataset):
        try:
            left, bottom, right, top = dataset.rio.bounds()
            return (left, bottom, right, top)
        except Exception:
            pass

    # Fallback: read from spatial coordinates
    if "x" in dataset.coords and "y" in dataset.coords:
        x = dataset.coords["x"].values
        y = dataset.coords["y"].values
        return (float(x.min()), float(y.min()), float(x.max()), float(y.max()))

    if "lon" in dataset.coords and "lat" in dataset.coords:
        lon = dataset.coords["lon"].values
        lat = dataset.coords["lat"].values
        return (float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max()))

    raise ValueError(
        "Cannot determine spatial bounds. Dataset must have .rio accessor or 'x'/'y' (or 'lon'/'lat') coordinates."
    )


def _resolve_resampling(name: str) -> Resampling:
    """Resolve a resampling method name to a rasterio Resampling enum."""
    key = name.strip().lower()
    if key not in _RESAMPLING_MAP:
        msg = f"Unsupported resampling method '{name}'. Choose from: {', '.join(sorted(_RESAMPLING_MAP))}"
        raise ValueError(msg)
    return _RESAMPLING_MAP[key]


class Reprojector:
    """Handles coordinate reference system reprojection and resampling.

    Attributes:
        target_crs: Target CRS string (e.g., "EPSG:4326").
        target_resolution: Target spatial resolution in CRS units.
        resampling_method: Default resampling method for raster data.
        variable_resampling: Per-variable resampling method overrides.
    """

    def __init__(
        self,
        target_crs: str = "EPSG:4326",
        target_resolution: float = 0.016666666666666666,
        resampling_method: str = "average",
        variable_resampling: dict[str, str] | None = None,
    ) -> None:
        """Initialize the reprojector.

        Args:
            target_crs: Target coordinate reference system.
            target_resolution: Target resolution in CRS units.
            resampling_method: Default resampling method.
            variable_resampling: Per-variable overrides, e.g.
                ``{"flood_fraction": "average", "quality_mask": "mode"}``.
        """
        self.target_crs = target_crs
        self.target_resolution = target_resolution
        self.resampling_method = resampling_method
        self.variable_resampling = variable_resampling or {}

    # ── Public API ────────────────────────────────────────────────────────

    def reproject(self, dataset: "xr.Dataset", source_crs: "CRS | str | None" = None) -> "xr.Dataset":
        """Reproject / resample an xarray Dataset to the target grid.

        When source and target CRS are the same (e.g. both EPSG:4326),
        this is a pure grid resampling operation — no actual warp is needed.
        ``rasterio.warp.reproject()`` handles this efficiently as a resample.

        Args:
            dataset: Input xarray Dataset with spatial coordinates.
            source_crs: Source CRS. If ``None``, attempts to detect from dataset.

        Returns:
            Reprojected / resampled xarray Dataset.

        Raises:
            ImportError: If rioxarray is needed but not installed.
            ValueError: If bounds cannot be determined.
        """
        if not dataset.data_vars:
            return dataset.copy()

        # ── 1. Determine CRS ──────────────────────────────────────────────
        src_crs = self._resolve_source_crs(dataset, source_crs)
        dst_crs = str(self.target_crs)

        # ── 2. Compute target grid ────────────────────────────────────────
        west, south, east, north = _get_dataset_bounds(dataset)

        dst_width = max(1, int(round((east - west) / self.target_resolution)))
        dst_height = max(1, int(round((north - south) / self.target_resolution)))
        dst_transform = from_bounds(west, south, east, north, dst_width, dst_height)

        # ── 3. Reproject each data variable ───────────────────────────────
        reprojected: dict[str, xr.DataArray] = {}
        for var_name in dataset.data_vars:
            da = dataset[var_name]
            method = self.variable_resampling.get(var_name, self.resampling_method)
            resampling = _resolve_resampling(method)

            src_array = self._prepare_array(da)
            dst_array = self._reproject_var(
                src_array,
                da,
                dst_height,
                dst_width,
                dst_transform,
                dst_crs,
                src_crs,
                resampling,
                method,
            )
            reprojected[var_name] = dst_array

        # ── 4. Build output dataset ───────────────────────────────────────
        ds_out = self._build_dataset(reprojected, west, north, dst_width, dst_height, dst_transform, dataset.attrs)
        return ds_out

    def validate_crs(self, dataset: "xr.Dataset") -> bool:
        """Validate that dataset has a valid CRS.

        Args:
            dataset: Input dataset to validate.

        Returns:
            True if valid CRS is present and matches target (no actual
            reprojection needed), False otherwise.
        """
        if not _rio_available(dataset):
            return False
        try:
            src = str(dataset.rio.crs)
        except Exception:
            return False
        return src == self.target_crs

    # ── Internal helpers ──────────────────────────────────────────────────

    def _resolve_source_crs(self, dataset: "xr.Dataset", source_crs: "CRS | str | None") -> str:
        """Determine the source CRS from parameter, accessor, or default."""
        if source_crs is not None:
            return str(source_crs)
        if _rio_available(dataset):
            try:
                return str(dataset.rio.crs)
            except Exception:
                pass
        return str(self.target_crs)

    @staticmethod
    def _prepare_array(da: "xr.DataArray") -> np.ndarray:
        """Extract a 2-D numpy array from a DataArray, squeezing band dims."""
        arr = da.values
        while arr.ndim > 2 and arr.shape[0] == 1:
            arr = arr[0]
        if arr.ndim != 2:
            raise ValueError(f"Expected 2-D data after squeeze, got shape {arr.shape} for '{da.name}'")
        return arr

    @staticmethod
    def _reproject_var(
        src_array: np.ndarray,
        src_da: "xr.DataArray",
        dst_height: int,
        dst_width: int,
        dst_transform: rasterio.Affine,
        dst_crs: str,
        src_crs: str,
        resampling: Resampling,
        method_name: str,
    ) -> "xr.DataArray":
        """Reproject a single variable array to the target grid."""
        import xarray as xr

        # Determine output dtype
        if method_name in ("mode", "nearest") and np.issubdtype(src_array.dtype, np.integer):
            out_dtype = src_array.dtype
        elif method_name == "average":
            out_dtype = np.float32
        else:
            out_dtype = np.float32

        destination = np.full((dst_height, dst_width), np.nan, dtype=np.float64)

        rio_reproject(
            source=src_array.astype(np.float64),
            destination=destination,
            src_transform=_get_transform(src_da),
            src_crs=src_crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            resampling=resampling,
        )

        # Mask NaN back to original nodata if integer
        dst_data: np.ndarray
        if np.issubdtype(out_dtype, np.integer):
            dst_data = np.nan_to_num(destination, nan=0).astype(out_dtype)
        else:
            dst_data = destination.astype(out_dtype)

        return xr.DataArray(
            dst_data,
            dims=["y", "x"],
            attrs=src_da.attrs,
            name=src_da.name,
        )

    @staticmethod
    def _build_dataset(
        variables: dict[str, "xr.DataArray"],
        west: float,
        north: float,
        dst_width: int,
        dst_height: int,
        dst_transform: rasterio.Affine,
        attrs: dict[str, Any],
    ) -> "xr.Dataset":
        """Assemble the output xarray Dataset with spatial coordinates."""
        import xarray as xr

        ds = xr.Dataset(variables, attrs=attrs)

        # Attach spatial coordinates matching the new grid
        x_coords = west + (np.arange(dst_width) + 0.5) * dst_transform.a
        y_coords = north - (np.arange(dst_height) + 0.5) * abs(dst_transform.e)
        ds = ds.assign_coords(x=x_coords, y=y_coords)

        # Write CRS and transform via rioxarray
        ds.rio.write_crs(ds.rio.crs or "EPSG:4326", inplace=True)
        ds.rio.write_transform(dst_transform, inplace=True)

        # Record processing metadata
        ds.attrs["processing"] = "harmonised"
        ds.attrs["target_resolution"] = dst_transform.a
        return ds


def _get_transform(da: "xr.DataArray") -> rasterio.Affine:
    """Safely retrieve the affine transform from a DataArray."""
    try:
        return da.rio.transform()
    except Exception:
        # Fallback: build a simple pixel-coordinate transform
        x = da.coords["x"].values if "x" in da.coords else da.coords["lon"].values
        y = da.coords["y"].values if "y" in da.coords else da.coords["lat"].values
        return from_bounds(float(x.min()), float(y.min()), float(x.max()), float(y.max()), len(x), len(y))
