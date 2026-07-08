"""Harmoniser for standardising flood data across sources."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from loguru import logger

from atlantis.config import HarmoniseConfig
from atlantis.harmoniser.normaliser import Normaliser, NormaliserConfig
from atlantis.harmoniser.reprojector import Reprojector
from atlantis.harmoniser.tiler import Tiler

if TYPE_CHECKING:
    import xarray as xr

#: Nodata sentinel for harmonised uint8 outputs.
HARMONISED_NODATA: int = 255

__all__ = [
    "HARMONISED_NODATA",
    "Harmoniser",
    "Reprojector",
    "Tiler",
    "Normaliser",
    "NormaliserConfig",
    "discover_nodata",
    "write_harmonised_raster",
]


def discover_nodata(data_array: "xr.DataArray") -> float | None:
    """Best-effort discovery of a DataArray's recorded nodata sentinel.

    Inspects explicit metadata attributes (``_FillValue`` / ``nodata`` /
    ``missing_value``) in order, then falls back to rioxarray's ``.rio.nodata``.
    Returns the first value that converts to ``float`` (which may be ``NaN``
    when that is the declared fill), or ``None`` when no sentinel is recorded.
    Callers apply their own dtype- or domain-specific fallback.
    """
    for key in ("_FillValue", "nodata", "missing_value"):
        value = data_array.attrs.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue

    try:
        rio_nodata = data_array.rio.nodata
        if rio_nodata is not None:
            return float(rio_nodata)
    except Exception:
        pass

    return None


def _integer_nodata(data_array: "xr.DataArray") -> int:
    """Return the integer nodata sentinel to preserve when writing code rasters."""
    discovered = discover_nodata(data_array)
    if discovered is None or np.isnan(discovered):
        return HARMONISED_NODATA
    return int(discovered)


def write_harmonised_raster(data_array: "xr.DataArray", output_path: Path | str) -> None:
    """Write a harmonised DataArray to a uint8 GeoTIFF.

    Flood fraction values in [0, 1] are scaled to [0, 100] (percent).
    NaN pixels are written as nodata=255. Integer code rasters preserve their
    existing nodata sentinel when one is present; otherwise they default to 255.

    Args:
        data_array: The xarray DataArray to write (float32 in [0,1] or uint8 binary).
        output_path: Destination file path.
    """
    arr = data_array.values
    nodata = HARMONISED_NODATA
    if np.issubdtype(arr.dtype, np.floating):
        # Scale [0, 1] → [0, 100], NaN → 255
        scaled = np.where(np.isnan(arr), HARMONISED_NODATA, np.round(arr * 100)).astype(np.uint8)
    else:
        # Integer masks / code rasters stay byte-for-byte unchanged.
        scaled = arr.astype(np.uint8)
        nodata = _integer_nodata(data_array)

    out_da = data_array.copy(data=scaled)
    out_da.rio.write_nodata(nodata, inplace=True)
    out_da.rio.to_raster(
        str(output_path),
        dtype="uint8",
        compress="LZW",
    )


class Harmoniser:
    """Orchestrates the harmonisation pipeline: reproject → normalise.

    Usage::

        harmoniser = Harmoniser(config=HarmoniseConfig())
        ds_harmonised = harmoniser.harmonise(ds, source_id="viirs")
    """

    def __init__(
        self,
        config: HarmoniseConfig | None = None,
        reprojector: Reprojector | None = None,
        normaliser: Normaliser | None = None,
    ) -> None:
        """Initialise the harmoniser.

        Args:
            config: Harmonisation configuration. If None, uses defaults.
            reprojector: Pre-configured Reprojector instance. If None, one is
                created from *config*.
            normaliser: Pre-configured Normaliser instance. If None, one is
                created from *config*.
        """
        self.config = config or HarmoniseConfig()
        self.reprojector = reprojector or Reprojector(
            target_crs=self.config.target_crs,
            target_resolution=self.config.target_resolution,
            resampling_method=self.config.resampling,
            variable_resampling=dict(self.config.variable_resampling),
            snap_to_global_grid=self.config.snap_to_global_grid,
            global_grid_origin_lon=self.config.global_grid_origin_lon,
            global_grid_origin_lat=self.config.global_grid_origin_lat,
        )
        self.normaliser = normaliser or Normaliser(
            config=NormaliserConfig(
                normalise_range=self.config.normalise_range,
                clip=True,
            )
        )

    def harmonise(
        self,
        dataset: xr.Dataset,
        source_id: str = "viirs",
        flood_variable: str = "flood_fraction",
    ) -> xr.Dataset:
        """Run the full harmonisation pipeline on a dataset.

        Steps:
        1. **Reproject** — resample all variables to the target resolution/CRS.
        2. **Normalise** — scale the flood variable to ``[0, 1]``.
        3. **Exclusion mask** — generate or forward invalid/excluded pixels.
        4. **Reference water** — generate or forward reference-water data.

        Args:
            dataset: Input xarray Dataset (e.g. from ``VIIRSFetcher.to_dataset()``).
            source_id: Source identifier (``"viirs"``, ``"gfm"``, etc.).
            flood_variable: Name of the flood extent variable to normalise.

        Returns:
            Harmonised xarray Dataset at the target resolution with
            normalised flood values and quality masks.
        """
        ds = dataset.copy(deep=True)

        # ── Warn if harmonising raw integer codes ─────────────────────
        if flood_variable == "raw":
            logger.warning(
                "Harmonising raw VIIRS codes: nearest-neighbour resampling preserves codes "
                "but the result is not a continuous flood fraction. "
                "Use --classify for semantically meaningful harmonised output."
            )

        # ── Step 1: Reproject / resample ──────────────────────────────
        ds = self.reprojector.reproject(ds)

        # ── Step 2: Normalise flood variable ──────────────────────────
        ds = self.normaliser.normalise(ds, variable=flood_variable)

        # ── Step 3: Exclusion mask ────────────────────────────────────
        if flood_variable in ds.data_vars:
            exclusion = self.normaliser.generate_exclusion_mask(ds, variable=flood_variable)
            ds["exclusion_mask"] = exclusion

        # ── Step 4: Reference water ───────────────────────────────────
        reference_water = self.normaliser.generate_reference_water(ds)
        ds["reference_water"] = reference_water

        # ── Record provenance ─────────────────────────────────────────
        ds.attrs["source_id"] = source_id
        ds.attrs["target_resolution_arcmin"] = self.config.target_resolution_arcmin
        ds.attrs["pipeline"] = "harmonise"
        return ds

    def harmonise_file(
        self,
        input_path: Path,
        output_path: Path,
        source_id: str = "viirs",
        flood_variable: str = "flood_fraction",
    ) -> Path:
        """Read a GeoTIFF, harmonise, and write the result.

        Convenience wrapper for single-file CLI usage.

        Args:
            input_path: Path to the input GeoTIFF.
            output_path: Path for the output GeoTIFF.
            source_id: Source identifier.
            flood_variable: Flood extent variable name.

        Returns:
            Path to the written output file.
        """
        import rioxarray as rxr

        ds = rxr.open_rasterio(input_path).squeeze(drop=True).to_dataset(name=flood_variable)
        ds_harm = self.harmonise(ds, source_id=source_id, flood_variable=flood_variable)

        # Write only the main flood variable
        write_harmonised_raster(ds_harm[flood_variable], output_path)
        return output_path
