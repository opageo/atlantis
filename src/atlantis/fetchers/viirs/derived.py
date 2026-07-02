"""VIIRS derived-layer definitions.

Each derived layer is a declarative spec plus a pure ``derive`` function of a
:class:`~atlantis.layers.DerivationContext`, registered on the VIIRS
:data:`~atlantis.fetchers.viirs.layers.registry`. Centralising the decode here
removes the duplication that previously existed between
``classify_viirs_pixels`` and ``ViirsRasterProcessor._classify_pixels``.
"""

from __future__ import annotations

import numpy as np

from atlantis.fetchers.viirs.layers import (
    CLOUD_CODES,
    FILL_CODES,
    PERMANENT_WATER_CODES,
    SELECTED_BAND,
    SHADOW_CODES,
    SNOW_ICE_CODES,
    registry,
)
from atlantis.layers import DerivationContext


def _invalid_mask(data: np.ndarray) -> np.ndarray:
    """Return True where the VIIRS code should be excluded from fractions."""
    return np.isin(data, list(FILL_CODES | CLOUD_CODES))


def _decode_fraction_codes(data: np.ndarray) -> np.ndarray:
    """Decode 100-200 fraction codes to float32, preserving fill/cloud as NaN."""
    invalid = _invalid_mask(data)
    out = np.zeros(data.shape, dtype=np.float32)
    out[invalid] = np.nan
    fraction_codes = (data >= 100) & (data <= 200)
    out[fraction_codes] = (data[fraction_codes].astype(np.float32) - 100.0) / 100.0
    return out


@registry.derived(
    name="water_fraction",
    inputs=(SELECTED_BAND,),
    dtype="float32",
    nodata=None,
    description=(
        "Continuous water fraction decoded from codes 100-200 as (code-100)/100, "
        "with NOAA reference-water code 99 and unquantified floodwater code 15 forced to 1.0. "
        "Fill and cloud pixels are NaN so temporal averaging skips them."
    ),
    resampling="average",
    aggregation="nanmean",
)
def water_fraction(ctx: DerivationContext) -> np.ndarray:
    """Decode the VIIRS band into the broader water-fraction product."""
    data = ctx[SELECTED_BAND]
    out = _decode_fraction_codes(data)
    out[np.isin(data, list(PERMANENT_WATER_CODES))] = 1.0
    out[data == 15] = 1.0
    return out


@registry.derived(
    name="flood_fraction",
    inputs=(SELECTED_BAND,),
    dtype="float32",
    nodata=None,
    description=(
        "Continuous fraction decoded directly from the NOAA 100-200 fraction codes. "
        "Reference-water code 99 and unquantified floodwater code 15 remain 0.0 here; "
        "fill and cloud pixels are NaN so temporal averaging skips them."
    ),
    resampling="average",
    aggregation="nanmean",
)
def flood_fraction(ctx: DerivationContext) -> np.ndarray:
    """Decode the VIIRS band into a float32 flood fraction, NaN for fill/cloud."""
    return _decode_fraction_codes(ctx[SELECTED_BAND])


@registry.derived(
    name="exclusion_mask",
    inputs=(SELECTED_BAND,),
    dtype="uint8",
    nodata=0,
    description=(
        "Exclusion mask: 1 for fill (0, 1) or cloud (30), 0 otherwise. "
        "Pre-existing water classes count as usable observations."
    ),
    resampling="mode",
    aggregation="mode",
)
def exclusion_mask(ctx: DerivationContext) -> np.ndarray:
    """Mark fill and cloud pixels excluded (1); everything else usable (0)."""
    return _invalid_mask(ctx[SELECTED_BAND]).astype(np.uint8)


@registry.derived(
    name="reference_water",
    inputs=(SELECTED_BAND,),
    dtype="uint8",
    nodata=0,
    description="Reference-water mask for NOAA NormalWater (code 99).",
    resampling="mode",
    aggregation="mode",
)
def reference_water(ctx: DerivationContext) -> np.ndarray:
    """Mark VIIRS reference-water pixels (code 99)."""
    data = ctx[SELECTED_BAND]
    out = np.zeros(data.shape, dtype=np.uint8)
    out[np.isin(data, list(PERMANENT_WATER_CODES))] = 1
    return out


@registry.derived(
    name="cloud_mask",
    inputs=(SELECTED_BAND,),
    dtype="uint8",
    nodata=0,
    description="Cloud mask (code 30): 1 where the pixel is cloud-covered.",
    resampling="mode",
    aggregation="mode",
)
def cloud_mask(ctx: DerivationContext) -> np.ndarray:
    """Mark cloud-covered pixels (code 30)."""
    data = ctx[SELECTED_BAND]
    out = np.zeros(data.shape, dtype=np.uint8)
    out[np.isin(data, list(CLOUD_CODES))] = 1
    return out


@registry.derived(
    name="snow_ice",
    inputs=(SELECTED_BAND,),
    dtype="uint8",
    nodata=0,
    description="Snow/ice mask (NOAA code 20).",
    resampling="mode",
    aggregation="mode",
)
def snow_ice(ctx: DerivationContext) -> np.ndarray:
    """Mark snow/ice pixels (code 20)."""
    data = ctx[SELECTED_BAND]
    out = np.zeros(data.shape, dtype=np.uint8)
    out[np.isin(data, list(SNOW_ICE_CODES))] = 1
    return out


@registry.derived(
    name="shadow",
    inputs=(SELECTED_BAND,),
    dtype="uint8",
    nodata=0,
    description="Terrain/cloud shadow mask (code 50) — flags low-confidence observations.",
    resampling="mode",
    aggregation="mode",
)
def shadow(ctx: DerivationContext) -> np.ndarray:
    """Mark shadow pixels (code 50)."""
    data = ctx[SELECTED_BAND]
    out = np.zeros(data.shape, dtype=np.uint8)
    out[np.isin(data, list(SHADOW_CODES))] = 1
    return out
