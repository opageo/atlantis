"""Harmoniser for standardising flood data across sources."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

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
    "write_harmonised_raster",
]


def write_harmonised_raster(data_array: "xr.DataArray", output_path: Path | str) -> None:
    """Write a harmonised DataArray to a uint8 GeoTIFF.

    Flood fraction values in [0, 1] are scaled to [0, 100] (percent).
    NaN pixels are written as nodata=255. Binary masks (quality, permanent
    water) are written as-is with the same nodata sentinel.

    Args:
        data_array: The xarray DataArray to write (float32 in [0,1] or uint8 binary).
        output_path: Destination file path.
    """
    arr = data_array.values
    if np.issubdtype(arr.dtype, np.floating):
        # Scale [0, 1] → [0, 100], NaN → 255
        scaled = np.where(np.isnan(arr), HARMONISED_NODATA, np.round(arr * 100)).astype(np.uint8)
    else:
        # Already integer (quality_mask, permanent_water) — just set nodata
        scaled = arr.astype(np.uint8)

    out_da = data_array.copy(data=scaled)
    out_da.rio.write_nodata(HARMONISED_NODATA, inplace=True)
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
        3. **Quality mask** — generate or forward quality flags.
        4. **Permanent water mask** — generate or forward permanent water.

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
            logging.getLogger(__name__).warning(
                "Harmonising raw VIIRS codes: nearest-neighbour resampling preserves codes "
                "but the result is not a continuous flood fraction. "
                "Use --classify for semantically meaningful harmonised output."
            )

        # ── Step 1: Reproject / resample ──────────────────────────────
        ds = self.reprojector.reproject(ds)

        # ── Step 2: Normalise flood variable ──────────────────────────
        ds = self.normaliser.normalise(ds, variable=flood_variable)

        # ── Step 3: Quality mask ──────────────────────────────────────
        if flood_variable in ds.data_vars:
            qm = self.normaliser.generate_quality_mask(ds, variable=flood_variable)
            ds["quality_mask"] = qm

        # ── Step 4: Permanent water mask ──────────────────────────────
        pw = self.normaliser.generate_permanent_water_mask(ds)
        ds["permanent_water"] = pw

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
