"""GFM derived-layer definitions.

Each derived layer is a declarative spec plus a pure ``derive`` function of a
:class:`~atlantis.layers.DerivationContext`, registered on the GFM
:data:`~atlantis.fetchers.gfm.layers.registry`. GFM derivations consume the
accumulated per-class coverage counts (not raw codes), so the context exposes
``flood_count`` / ``perm_water_count`` / ``valid_count``.
"""

from __future__ import annotations

import numpy as np

from atlantis.fetchers.gfm.layers import (
    FLOOD_COUNT,
    PERM_WATER_COUNT,
    VALID_COUNT,
    registry,
)
from atlantis.layers import DerivationContext


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
    flood_count = ctx[FLOOD_COUNT]
    valid_count = ctx[VALID_COUNT]
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(
            valid_count > 0,
            flood_count.astype(np.float32) / valid_count.astype(np.float32),
            np.nan,
        ).astype(np.float32)


@registry.derived(
    name="quality_mask",
    inputs=(VALID_COUNT,),
    dtype="uint8",
    nodata=255,
    description="Observation-coverage mask: 1 where at least one valid SAR observation contributed.",
    resampling="mode",
    aggregation="any",
)
def quality_mask(ctx: DerivationContext) -> np.ndarray:
    """Mark pixels with at least one valid observation as good (1)."""
    return (ctx[VALID_COUNT] > 0).astype(np.uint8)


@registry.derived(
    name="permanent_water",
    inputs=(PERM_WATER_COUNT, VALID_COUNT),
    dtype="uint8",
    nodata=255,
    description="Permanent-water mask: majority (>50%) of observed coverage is permanent water.",
    resampling="mode",
    aggregation="majority",
)
def permanent_water(ctx: DerivationContext) -> np.ndarray:
    """Mark pixels where permanent water dominates the observed coverage."""
    valid_count = ctx[VALID_COUNT]
    perm_water_count = ctx[PERM_WATER_COUNT]
    with np.errstate(divide="ignore", invalid="ignore"):
        perm_ratio = np.where(
            valid_count > 0,
            perm_water_count.astype(np.float32) / valid_count.astype(np.float32),
            0.0,
        )
    return (perm_ratio > 0.5).astype(np.uint8)
