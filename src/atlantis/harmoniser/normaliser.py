"""Normaliser for value normalisation and mask generation."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import xarray as xr


@dataclass
class NormaliserConfig:
    """Configuration for value normalisation.

    Attributes:
        normalise_range: Tuple of (min, max) for normalisation (0.0-1.0 default).
        fill_value: Value to use for missing data.
        clip: Whether to clip values outside normalise_range.
    """

    normalise_range: tuple[float, float] = (0.0, 1.0)
    fill_value: float = -9999.0
    clip: bool = True


class Normaliser:
    """Handles value normalisation and quality mask generation.

    Standardises flood extent values to 0-1 range and generates
    quality masks for ML training.
    """

    def __init__(self, config: NormaliserConfig | None = None) -> None:
        """Initialize the normaliser.

        Args:
            config: Normalisation configuration. Uses defaults if None.
        """
        self.config = config or NormaliserConfig()

    def normalise(self, dataset: "xr.Dataset", variable: str = "flood_extent") -> "xr.Dataset":
        """Normalise variable values to configured range.

        Args:
            dataset: Input xarray Dataset.
            variable: Variable name to normalise.

        Returns:
            Dataset with normalised values.

        Raises:
            KeyError: If variable not found in dataset.
        """
        # TODO: Implement normalisation
        # Expected implementation:
        # 1. Get variable data
        # 2. Replace fill_value with NaN
        # 3. Scale to normalise_range
        # 4. Optionally clip to range
        # 5. Return updated dataset
        raise NotImplementedError("Normalisation not yet implemented")

    def generate_quality_mask(self, dataset: "xr.Dataset", variable: str = "flood_extent") -> "xr.DataArray":
        """Generate a quality mask from data values.

        Args:
            dataset: Input xarray Dataset.
            variable: Variable to base mask on.

        Returns:
            DataArray with uint8 quality flags:
            - 0: valid data
            - 1: no data / fill value
            - 2: cloud contaminated
            - 4: snow contaminated
            - 8: outside area of interest
        """
        # TODO: Implement quality mask generation
        # Expected implementation:
        # 1. Identify fill values / NaN -> mask value 1
        # 2. Check for cloud flags in metadata -> mask value 2
        # 3. Check for snow flags in metadata -> mask value 4
        # 4. Return combined mask as uint8
        raise NotImplementedError("Quality mask generation not yet implemented")

    def generate_permanent_water_mask(self, dataset: "xr.Dataset") -> "xr.DataArray":
        """Generate permanent water mask from reference data.

        Args:
            dataset: Input xarray Dataset with permanent_water variable.

        Returns:
            DataArray with permanent water (1) and non-water (0).
        """
        # TODO: Implement permanent water mask
        # Expected implementation:
        # 1. Extract permanent_water variable if present
        # 2. Convert to binary mask (1 = water, 0 = land)
        # 3. Return as uint8 DataArray
        raise NotImplementedError("Permanent water mask not yet implemented")
