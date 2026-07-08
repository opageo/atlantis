"""GFM derived-layer definitions.

Each derived layer is a declarative spec plus a pure ``derive`` function of a
:class:`~atlantis.layers.DerivationContext`, registered on the GFM
:data:`~atlantis.fetchers.gfm.layers.registry`. GFM derivations consume the
accumulated per-class coverage counts (not raw codes), so the context exposes
``ensemble_flood_extent_count`` / ``ensemble_water_extent_count`` /
``valid_count`` plus the native ``reference_water_mask`` codes under
``reference_water_mask_codes``. Each accumulator is named after the native
band it is built from (mean-pooled, reprojected, and summed across the SAR
observations in a date group) — see :mod:`atlantis.fetchers.gfm.layers` for
the exact accumulation rule.
"""

from __future__ import annotations

import numpy as np

from atlantis.fetchers.gfm.layers import (
    ENSEMBLE_FLOOD_EXTENT_COUNT,
    ENSEMBLE_WATER_EXTENT_COUNT,
    REFERENCE_WATER_MASK_CODES,
    VALID_COUNT,
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
    inputs=(ENSEMBLE_WATER_EXTENT_COUNT, VALID_COUNT),
    dtype="float32",
    nodata=None,
    description=(
        "Fraction of valid SAR observations flagged as water "
        "(ensemble_water_extent_count / valid_count); ensemble_water_extent_count is "
        "accumulated from native ensemble_water_extent, and valid_count from the "
        "combined per-pixel validity of ensemble_flood_extent, ensemble_water_extent, "
        "and reference_water_mask, across the date group; NaN where unobserved."
    ),
    resampling="average",
    aggregation="nanmean",
)
def water_fraction(ctx: DerivationContext) -> np.ndarray:
    """Derive the per-pixel observed water fraction from accumulated counts."""
    return _fraction_from_count(ctx[ENSEMBLE_WATER_EXTENT_COUNT], ctx[VALID_COUNT])


@registry.derived(
    name="flood_fraction",
    inputs=(ENSEMBLE_FLOOD_EXTENT_COUNT, VALID_COUNT),
    dtype="float32",
    nodata=None,
    description=(
        "Fraction of valid SAR observations flagged as flood "
        "(ensemble_flood_extent_count / valid_count); ensemble_flood_extent_count is "
        "accumulated from native ensemble_flood_extent, and valid_count from the "
        "combined per-pixel validity of ensemble_flood_extent, ensemble_water_extent, "
        "and reference_water_mask, across the date group; NaN where unobserved."
    ),
    resampling="average",
    aggregation="nanmean",
)
def flood_fraction(ctx: DerivationContext) -> np.ndarray:
    """Derive the per-pixel observed flood fraction from accumulated counts."""
    return _fraction_from_count(ctx[ENSEMBLE_FLOOD_EXTENT_COUNT], ctx[VALID_COUNT])


@registry.derived(
    name="reference_water",
    inputs=(REFERENCE_WATER_MASK_CODES,),
    dtype="uint8",
    nodata=255,
    description=(
        "Reference-water codes carried through from native reference_water_mask "
        "(masked-max across the date group) under the shared layer name."
    ),
    resampling="nearest",
    aggregation="masked_max",
)
def reference_water(ctx: DerivationContext) -> np.ndarray:
    """Carry the native reference-water codes under the shared layer name."""
    return ctx[REFERENCE_WATER_MASK_CODES].astype(np.uint8)
