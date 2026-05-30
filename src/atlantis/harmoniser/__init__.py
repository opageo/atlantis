"""Harmoniser for standardising flood data across sources."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from atlantis.config import HarmoniseConfig
from atlantis.harmoniser.normaliser import Normaliser, NormaliserConfig
from atlantis.harmoniser.reprojector import Reprojector
from atlantis.harmoniser.tiler import Tiler

if TYPE_CHECKING:
    import xarray as xr

__all__ = [
    "Harmoniser",
    "Reprojector",
    "Tiler",
    "Normaliser",
    "NormaliserConfig",
]


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
        flood_variable: str = "flood_extent",
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
        flood_variable: str = "flood_extent",
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
        ds_harm[flood_variable].rio.to_raster(
            str(output_path),
            dtype="float32",
            compress="LZW",
            nodata=float("nan"),
        )
        return output_path
