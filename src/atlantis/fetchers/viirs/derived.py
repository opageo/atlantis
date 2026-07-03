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


@registry.derived(
    name="flood_fraction",
    inputs=(SELECTED_BAND,),
    dtype="float32",
    nodata=None,
    description=(
        "Continuous water fraction decoded from codes 101-200 as (code-100)/100. "
        "Valid non-flood observations map to 0.0; fill and cloud pixels are NaN so "
        "temporal averaging skips them."
    ),
    resampling="average",
    aggregation="nanmean",
)
def flood_fraction(ctx: DerivationContext) -> np.ndarray:
    """Decode the VIIRS band into a float32 flood fraction, NaN for fill/cloud."""
    data = ctx[SELECTED_BAND]
    missing = np.isin(data, list(FILL_CODES | CLOUD_CODES))
    flood_mask = (data >= 101) & (data <= 200)
    out = np.full(data.shape, np.nan, dtype=np.float32)
    out[~missing] = 0.0
    out[flood_mask] = (data[flood_mask].astype(np.float32) - 100.0) / 100.0
    return out


@registry.derived(
    name="quality_mask",
    inputs=(SELECTED_BAND,),
    dtype="uint8",
    nodata=0,
    description=(
        "Valid clear-sky observation mask: 0 for fill (0, 1) or cloud (30), 1 otherwise. "
        "Pre-existing water classes count as valid observations."
    ),
    resampling="mode",
    aggregation="mode",
)
def quality_mask(ctx: DerivationContext) -> np.ndarray:
    """Mark fill and cloud pixels invalid (0); everything else valid (1)."""
    data = ctx[SELECTED_BAND]
    invalid = np.isin(data, list(FILL_CODES | CLOUD_CODES))
    out = np.ones(data.shape, dtype=np.uint8)
    out[invalid] = 0
    return out


@registry.derived(
    name="permanent_water",
    inputs=(SELECTED_BAND,),
    dtype="uint8",
    nodata=0,
    description="Reference (NormalWater, code 99) permanent-water mask.",
    resampling="mode",
    aggregation="mode",
)
def permanent_water(ctx: DerivationContext) -> np.ndarray:
    """Mark VIIRS reference-water pixels (code 99) as permanent water."""
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
