"""Normaliser for value normalisation and mask generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import xarray as xr


@dataclass
class NormaliserConfig:
    """Configuration for value normalisation.

    Attributes:
        normalise_range: Tuple of (min, max) for normalisation (0.0-1.0 default).
        fill_value: Value to use for missing data.
        clip: Whether to clip values outside normalise_range.
        skip_normalise_vars: Set of variable names to skip normalisation for
            (e.g. quality_mask, permanent_water — already binary).
    """

    normalise_range: tuple[float, float] = (0.0, 1.0)
    fill_value: float = -9999.0
    clip: bool = True
    skip_normalise_vars: set[str] = field(default_factory=lambda: {"quality_mask", "permanent_water"})


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

    def normalise(self, dataset: "xr.Dataset", variable: str = "flood_extent") -> "xr.Dataset":
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
        if variable in self.config.skip_normalise_vars:
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

    def generate_quality_mask(self, dataset: "xr.Dataset", variable: str = "flood_extent") -> "xr.DataArray":
        """Generate a quality mask from data values.

        The mask is constructed from multiple sources of information:
        - A ``quality_mask`` variable in the dataset is used directly if present
        - Otherwise derived from fill / NaN detection on *variable*
        - Cloud flags from dataset metadata (``cloud_fraction`` attribute) are
          incorporated as a flag when contaminated

        Returns:
            DataArray with uint8 quality flags:
            - 0: valid data
            - 1: no data / fill value
            - 2: cloud contaminated
            - 4: snow contaminated
            - 8: outside area of interest
        """
        import xarray as xr

        # ── Use existing quality_mask variable if available ────────────
        if "quality_mask" in dataset.data_vars:
            qm = dataset["quality_mask"].values.astype(np.uint8)
            # quality_mask from VIIRS: 1=good, 0=bad → invert to flags
            mask = np.zeros_like(qm, dtype=np.uint8)
            mask[qm == 0] = 1  # no data / fill
        else:
            # ── Derive from variable fill values / NaN ────────────────
            da = dataset[variable]
            data = da.values
            mask = np.zeros(data.shape, dtype=np.uint8)

            # NaN / fill detection
            if np.issubdtype(data.dtype, np.floating):
                mask[np.isnan(data)] = 1
            elif self.config.fill_value is not None:
                mask[data == self.config.fill_value] = 1

        # ── Cloud contamination from metadata ──────────────────────────
        cloud_frac = dataset.attrs.get("cloud_fraction", 0.0)
        if cloud_frac > 0.1:
            # Flag whole scene as potentially cloud-contaminated
            # (pixel-level cloud mask is only available from the source quality mask)
            mask[mask == 0] = 2

        return xr.DataArray(
            mask,
            dims=dataset[variable].dims,
            coords=dataset[variable].coords,
            attrs={"description": "Quality flags: 0=valid, 1=nodata, 2=cloud, 4=snow, 8=outside"},
            name="quality_mask",
        )

    def generate_permanent_water_mask(self, dataset: "xr.Dataset") -> "xr.DataArray":
        """Generate permanent water mask from reference data.

        Args:
            dataset: Input xarray Dataset with a ``permanent_water`` variable
                (from VIIRS classification) or from which a mask can be derived.

        Returns:
            DataArray with permanent water (1) and non-water (0) as uint8.
        """
        import xarray as xr

        if "permanent_water" in dataset.data_vars:
            pw = dataset["permanent_water"].values.astype(np.uint8)
            pw_mask = np.where(pw > 0, 1, 0).astype(np.uint8)
        else:
            pw_mask = np.zeros(
                _shape_from_dataset(dataset),
                dtype=np.uint8,
            )

        return xr.DataArray(
            pw_mask,
            dims=dataset[list(dataset.data_vars)[0]].dims,
            coords=dataset[list(dataset.data_vars)[0]].coords,
            attrs={"description": "Permanent water mask: 1=water, 0=land"},
            name="permanent_water",
        )


def _shape_from_dataset(dataset: "xr.Dataset") -> tuple[int, ...]:
    """Infer the spatial shape from the first data variable."""
    for var in dataset.data_vars.values():
        return var.shape[-2:]
    return (0, 0)
