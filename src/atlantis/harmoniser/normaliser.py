"""Normaliser for value normalisation and mask generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
from loguru import logger

from atlantis.layers import is_known_layer

if TYPE_CHECKING:
    import xarray as xr


@dataclass
class NormaliserConfig:
    """Configuration for value normalisation.

    Attributes:
        normalise_range: Tuple of (min, max) for normalisation (0.0-1.0 default).
        fill_value: Value to use for missing data.
        clip: Whether to clip values outside normalise_range.
        skip_normalise_vars: Set of variable names to skip normalisation for.
            ``water_fraction`` and ``flood_fraction`` are included because they
            are already physical fractions in ``[0, 1]``. A per-image min-max
            stretch would corrupt their physical meaning. With ``clip=True`` the
            harmoniser still enforces the ``[0, 1]`` boundary defensively
            without rescaling. ``exclusion_mask``, ``reference_water``, and
            ``raw`` are skipped because they carry discrete codes that must not
            be stretched.
    """

    normalise_range: tuple[float, float] = (0.0, 1.0)
    fill_value: float = -9999.0
    clip: bool = True
    skip_normalise_vars: set[str] = field(
        default_factory=lambda: {
            "water_fraction",
            "flood_fraction",
            "exclusion_mask",
            "reference_water",
            # Legacy aliases retained for compatibility with older callers/tests.
            "quality_mask",
            "permanent_water",
            "raw",
        }
    )


class Normaliser:
    """Handles value normalisation and quality mask generation.

    Standardises flood extent values to a 0–1 range and generates
    quality masks for ML training.
    """

    def __init__(self, config: NormaliserConfig | None = None) -> None:
        """Initialize the normaliser.

        Args:
            config: Normalisation configuration. Uses defaults if None.
        """
        self.config = config or NormaliserConfig()

    # ── Public API ────────────────────────────────────────────────────────

    def normalise(self, dataset: "xr.Dataset", variable: str = "flood_fraction") -> "xr.Dataset":
        """Normalise variable values to the configured range.

        Args:
            dataset: Input xarray Dataset.
            variable: Variable name to normalise. Skips if the variable is
                in ``config.skip_normalise_vars``.

        Returns:
            Dataset with normalised values.

        Raises:
            KeyError: If variable not found in dataset.
        """
        import xarray as xr

        if variable not in dataset.data_vars:
            raise KeyError(f"Variable '{variable}' not found in dataset. Available: {list(dataset.data_vars)}")

        ds = dataset.copy(deep=True)
        da = ds[variable]

        # ── Skip normalisation for already-binary masks ────────────────
        # Any registered layer (native code band or derived fraction/mask) is
        # either a physical fraction or a discrete code that must not be
        # min-max stretched, so registry membership extends the explicit set.
        if variable in self.config.skip_normalise_vars or is_known_layer(variable, ds.attrs.get("source_id")):
            logger.debug("Skipping normalisation for '{}' (known layer / skip list)", variable)
            ds.attrs["normalisation_skipped"] = variable
            return ds

        # ── Replace fill_value with NaN ────────────────────────────────
        data = da.values.astype(np.float32)
        fill = self.config.fill_value
        if not np.isnan(fill):
            data[data == fill] = np.nan

        # ── Scale data to the normalise_range ──────────────────────────
        lo, hi = self.config.normalise_range
        dmin, dmax = float(np.nanmin(data)), float(np.nanmax(data))
        logger.debug(
            "Normalising '{}': data range [{:.4f}, {:.4f}] -> target [{:.1f}, {:.1f}]",
            variable,
            dmin,
            dmax,
            lo,
            hi,
        )

        if np.isnan(dmin) or np.isnan(dmax) or np.isclose(dmax - dmin, 0.0):
            # All NaN or constant — no scaling needed
            normalised = data
        else:
            normalised = (data - dmin) / (dmax - dmin)  # → [0, 1]
            if lo != 0.0 or hi != 1.0:
                normalised = normalised * (hi - lo) + lo  # → [lo, hi]

        # ── Clip if configured ─────────────────────────────────────────
        if self.config.clip:
            normalised = np.clip(normalised, lo, hi)

        ds[variable] = xr.DataArray(
            normalised,
            dims=da.dims,
            coords=da.coords,
            attrs=da.attrs,
            name=variable,
        )
        ds.attrs["normalisation_applied"] = variable
        return ds

    def generate_exclusion_mask(self, dataset: "xr.Dataset", variable: str = "flood_fraction") -> "xr.DataArray":
        """Generate or forward a binary exclusion mask.

        The returned mask uses the shared convention ``1 = excluded/invalid``
        and ``0 = usable``.
        """
        import xarray as xr

        if "exclusion_mask" in dataset.data_vars:
            mask = dataset["exclusion_mask"].values.astype(np.uint8)
        elif "quality_mask" in dataset.data_vars:
            qm = dataset["quality_mask"].values.astype(np.uint8)
            mask = np.where(qm > 0, 0, 1).astype(np.uint8)
        else:
            da = dataset[variable]
            data = da.values
            mask = np.zeros(data.shape, dtype=np.uint8)
            if np.issubdtype(data.dtype, np.floating):
                mask[np.isnan(data)] = 1
            elif self.config.fill_value is not None:
                mask[data == self.config.fill_value] = 1

        return xr.DataArray(
            mask,
            dims=dataset[variable].dims,
            coords=dataset[variable].coords,
            attrs={"description": "Exclusion mask: 1=excluded/invalid, 0=usable"},
            name="exclusion_mask",
        )

    def generate_reference_water(self, dataset: "xr.Dataset") -> "xr.DataArray":
        """Generate or forward the shared reference-water layer.

        Args:
            dataset: Input xarray Dataset with a ``reference_water`` or legacy
                ``permanent_water`` variable.

        Returns:
            DataArray carrying the reference-water values as uint8.
        """
        import xarray as xr

        if "reference_water" in dataset.data_vars:
            ref = dataset["reference_water"].values.astype(np.uint8)
        elif "permanent_water" in dataset.data_vars:
            ref = dataset["permanent_water"].values.astype(np.uint8)
        else:
            ref = np.zeros(
                _shape_from_dataset(dataset),
                dtype=np.uint8,
            )

        return xr.DataArray(
            ref,
            dims=dataset[list(dataset.data_vars)[0]].dims,
            coords=dataset[list(dataset.data_vars)[0]].coords,
            attrs={"description": "Reference water layer"},
            name="reference_water",
        )

    def generate_quality_mask(self, dataset: "xr.Dataset", variable: str = "flood_fraction") -> "xr.DataArray":
        """Backward-compatible wrapper for callers still requesting quality_mask."""
        import xarray as xr

        exclusion = self.generate_exclusion_mask(dataset, variable=variable)
        mask = exclusion.values.astype(np.uint8).copy()

        cloud_frac = dataset.attrs.get("cloud_fraction", 0.0)
        if cloud_frac > 0.1:
            mask[mask == 0] = 2

        return xr.DataArray(
            mask,
            dims=exclusion.dims,
            coords=exclusion.coords,
            attrs={"description": "Quality flags: 0=valid, 1=nodata, 2=cloud, 4=snow, 8=outside"},
            name="quality_mask",
        )

    def generate_permanent_water_mask(self, dataset: "xr.Dataset") -> "xr.DataArray":
        """Backward-compatible wrapper for callers still requesting permanent_water."""
        return self.generate_reference_water(dataset).rename("permanent_water")


def _shape_from_dataset(dataset: "xr.Dataset") -> tuple[int, ...]:
    """Infer the spatial shape from the first data variable."""
    for var in dataset.data_vars.values():
        return var.shape[-2:]
    return (0, 0)
