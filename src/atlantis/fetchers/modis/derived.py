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


def _fraction_for_codes(data: np.ndarray, water_codes: tuple[int, ...]) -> np.ndarray:
    """Return a float32 fraction mask for *water_codes*, NaN for insufficient data."""
    valid = data != INSUFFICIENT_DATA_CODE
    out = np.zeros(data.shape, dtype=np.float32)
    out[~valid] = np.nan
    out[np.isin(data, water_codes)] = 1.0
    return out


@registry.derived(
    name="water_fraction",
    inputs=(SELECTED_COMPOSITE,),
    dtype="float32",
    nodata=None,
    description=(
        "Binary water-observation fraction from classes 1/2/3 as float32; "
        "insufficient-data pixels (255) are NaN so downstream averaging yields a sub-pixel fraction."
    ),
    resampling="average",
    aggregation="nanmean",
)
def water_fraction(ctx: DerivationContext) -> np.ndarray:
    """Derive the float32 water fraction from the selected composite codes."""
    data = ctx[SELECTED_COMPOSITE]
    return _fraction_for_codes(
        data,
        (SURFACE_WATER_CODE, RECURRING_FLOOD_CODE, UNUSUAL_FLOOD_CODE),
    )


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
    return _fraction_for_codes(ctx[SELECTED_COMPOSITE], (UNUSUAL_FLOOD_CODE,))


@registry.derived(
    name="exclusion_mask",
    inputs=(SELECTED_COMPOSITE,),
    dtype="uint8",
    nodata=0,
    description=(
        "Exclusion / insufficient-data mask (composite == 255). 1 = excluded or invalid, 0 = usable classification."
    ),
    resampling="mode",
    aggregation="mode",
)
def exclusion_mask(ctx: DerivationContext) -> np.ndarray:
    """Derive the binary exclusion mask from the selected composite codes."""
    return (ctx[SELECTED_COMPOSITE] == INSUFFICIENT_DATA_CODE).astype(np.uint8)


@registry.derived(
    name="reference_water",
    inputs=(SELECTED_COMPOSITE,),
    dtype="uint8",
    nodata=0,
    description="Reference water mask (surface water or recurring flood: classes 1 and 2).",
    resampling="mode",
    aggregation="mode",
)
def reference_water(ctx: DerivationContext) -> np.ndarray:
    """Derive the MODIS reference-water mask from the selected composite codes."""
    data = ctx[SELECTED_COMPOSITE]
    return np.isin(data, (SURFACE_WATER_CODE, RECURRING_FLOOD_CODE)).astype(np.uint8)


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
