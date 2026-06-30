"""MODIS MCDWD derived-layer definitions.

Each derived layer is a declarative spec plus a pure ``derive`` function of a
:class:`~atlantis.layers.DerivationContext`. They are registered on the MODIS
:data:`~atlantis.fetchers.modis.layers.registry` so the processor, CLI, and docs
all read from one place. Keeping the math here (not inline in the processor)
removes duplication and makes every layer unit-testable in isolation.
"""

from __future__ import annotations

import numpy as np

from atlantis.fetchers.modis.layers import (
    INSUFFICIENT_DATA_CODE,
    RECURRING_FLOOD_CODE,
    SELECTED_COMPOSITE,
    SURFACE_WATER_CODE,
    UNUSUAL_FLOOD_CODE,
    registry,
)
from atlantis.layers import DerivationContext


@registry.derived(
    name="flood_fraction",
    inputs=(SELECTED_COMPOSITE,),
    dtype="float32",
    nodata=None,
    description=(
        "Binary unusual-flood flag (composite == 3) as float32; insufficient-data "
        "pixels (255) are NaN so the harmoniser's averaging yields a sub-pixel fraction."
    ),
    resampling="average",
    aggregation="nanmean",
)
def flood_fraction(ctx: DerivationContext) -> np.ndarray:
    """Derive the float32 flood fraction from the selected composite codes."""
    data = ctx[SELECTED_COMPOSITE]
    valid = data != INSUFFICIENT_DATA_CODE
    out = (data == UNUSUAL_FLOOD_CODE).astype(np.float32)
    out[~valid] = np.nan
    return out


@registry.derived(
    name="quality_mask",
    inputs=(SELECTED_COMPOSITE,),
    dtype="uint8",
    nodata=0,
    description=(
        "Valid-observation mask (composite != 255). 1 = usable classification, "
        "0 = insufficient data (always HAND/terrain-shadow masked; cloud handling "
        "is composite-specific)."
    ),
    resampling="mode",
    aggregation="mode",
)
def quality_mask(ctx: DerivationContext) -> np.ndarray:
    """Derive the binary validity mask from the selected composite codes."""
    return (ctx[SELECTED_COMPOSITE] != INSUFFICIENT_DATA_CODE).astype(np.uint8)


@registry.derived(
    name="permanent_water",
    inputs=(SELECTED_COMPOSITE,),
    dtype="uint8",
    nodata=0,
    description="Reference surface-water mask (composite == 1).",
    resampling="mode",
    aggregation="mode",
)
def permanent_water(ctx: DerivationContext) -> np.ndarray:
    """Derive the binary permanent-water mask from the selected composite codes."""
    return (ctx[SELECTED_COMPOSITE] == SURFACE_WATER_CODE).astype(np.uint8)


@registry.derived(
    name="recurring_flood",
    inputs=(SELECTED_COMPOSITE,),
    dtype="uint8",
    nodata=0,
    description="MODIS-only recurring-flood mask (composite == 2).",
    resampling="mode",
    aggregation="mode",
)
def recurring_flood(ctx: DerivationContext) -> np.ndarray:
    """Derive the MODIS-only recurring-flood mask from the selected composite codes."""
    return (ctx[SELECTED_COMPOSITE] == RECURRING_FLOOD_CODE).astype(np.uint8)
