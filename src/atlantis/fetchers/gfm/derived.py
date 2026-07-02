"""GFM derived-layer definitions.

Each derived layer is a declarative spec plus a pure ``derive`` function of a
:class:`~atlantis.layers.DerivationContext`, registered on the GFM
:data:`~atlantis.fetchers.gfm.layers.registry`. GFM derivations consume the
accumulated per-class coverage counts (not raw codes), so the context exposes
``flood_count`` / ``water_count`` / ``valid_count`` plus the native
``reference_water_mask`` codes.
"""

from __future__ import annotations

import numpy as np

from atlantis.fetchers.gfm.layers import (
    FLOOD_COUNT,
    REFERENCE_WATER_CODES,
    VALID_COUNT,
    WATER_COUNT,
    registry,
)
from atlantis.layers import DerivationContext


def _fraction_from_count(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    """Return a float32 fraction array, preserving unobserved pixels as NaN."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(
            denominator > 0,
            numerator.astype(np.float32) / denominator.astype(np.float32),
            np.nan,
        ).astype(np.float32)


@registry.derived(
    name="water_fraction",
    inputs=(WATER_COUNT, VALID_COUNT),
    dtype="float32",
    nodata=None,
    description=(
        "Fraction of valid SAR observations flagged as water (water_count / valid_count); NaN where unobserved."
    ),
    resampling="average",
    aggregation="nanmean",
)
def water_fraction(ctx: DerivationContext) -> np.ndarray:
    """Derive the per-pixel observed water fraction from accumulated counts."""
    return _fraction_from_count(ctx[WATER_COUNT], ctx[VALID_COUNT])


@registry.derived(
    name="flood_fraction",
    inputs=(FLOOD_COUNT, VALID_COUNT),
    dtype="float32",
    nodata=None,
    description=(
        "Fraction of valid SAR observations flagged as flood (flood_count / valid_count); NaN where unobserved."
    ),
    resampling="average",
    aggregation="nanmean",
)
def flood_fraction(ctx: DerivationContext) -> np.ndarray:
    """Derive the per-pixel observed flood fraction from accumulated counts."""
    return _fraction_from_count(ctx[FLOOD_COUNT], ctx[VALID_COUNT])


@registry.derived(
    name="reference_water",
    inputs=(REFERENCE_WATER_CODES,),
    dtype="uint8",
    nodata=255,
    description=("Reference-water codes carried through from GFM reference_water_mask under the shared layer name."),
    resampling="nearest",
    aggregation="max",
)
def reference_water(ctx: DerivationContext) -> np.ndarray:
    """Carry the native reference-water codes under the shared layer name."""
    return ctx[REFERENCE_WATER_CODES].astype(np.uint8)
