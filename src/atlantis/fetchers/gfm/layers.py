"""GFM (Global Flood Monitoring) native layer catalogue.

Single source of truth for *what GFM layers exist*. GFM publishes discrete
Sentinel-1 SAR code bands as STAC assets. Unlike MODIS/VIIRS, GFM derivations do
not read raw pixel codes directly: the processor accumulates per-class coverage
*counts* across the SAR observations in a date group, and the derived layers are
computed from those accumulators. The processor therefore exposes the counts to
derivations under the keys :data:`ENSEMBLE_FLOOD_EXTENT_COUNT` (accumulated from
native ``ensemble_flood_extent``), :data:`ENSEMBLE_WATER_EXTENT_COUNT`
(accumulated from native ``ensemble_water_extent``), and :data:`VALID_COUNT`
(accumulated validity of either band), plus the reprojected reference-water
codes under :data:`REFERENCE_WATER_MASK_CODES` (carried through from native
``reference_water_mask``).

Derived-layer definitions live in :mod:`atlantis.fetchers.gfm.derived` and are
imported at the end of this module so a single ``import ...gfm.layers`` populates
the full registry.
"""

from __future__ import annotations

from atlantis.layers import (
    LayerRegistry,
    NativeLayer,
    register_source_registry,
)

# ── GFM code constants (verified against EODC STAC COGs) ─────────────────
GFM_NODATA: int = 255
# ensemble_flood_extent codes
GFM_DRY: int = 0
GFM_FLOOD: int = 1
# ensemble_water_extent codes (0 = dry / not-water reuses GFM_DRY)
GFM_WATER: int = 1
# reference_water_mask codes — GFM PDD Table 20, confirmed against the live COGs
# and a month-stability test (code 1 is byte-identical across the monthly masks →
# permanent; code 2 varies by month → seasonal). NB: this module previously set
# GFM_PERMANENT_WATER = 2, which was backwards.
GFM_LAND: int = 0
GFM_PERMANENT_WATER: int = 1
GFM_SEASONAL_WATER: int = 2

#: Native bands loaded from each GFM STAC item.
GFM_BANDS: list[str] = [
    "ensemble_flood_extent",
    "ensemble_water_extent",
    "reference_water_mask",
    "exclusion_mask",
    "ensemble_likelihood",
    "advisory_flags",
]

#: Derivation input keys: accumulated per-class coverage counts (float32),
#: summed across the SAR observations in a date group (one contribution in
#: ``[0, 1]`` per observation). Named after the native band each accumulates,
#: except :data:`VALID_COUNT`, which combines the per-pixel validity of all
#: three core bands (``ensemble_flood_extent``, ``ensemble_water_extent``, and
#: ``reference_water_mask``) — an observation counts as valid if *any* of the
#: three has a non-nodata code.
ENSEMBLE_FLOOD_EXTENT_COUNT = "ensemble_flood_extent_count"
ENSEMBLE_WATER_EXTENT_COUNT = "ensemble_water_extent_count"
VALID_COUNT = "valid_count"
#: Reprojected ``reference_water_mask`` codes, carried through (masked-max)
#: across the SAR observations in a date group.
REFERENCE_WATER_MASK_CODES = "reference_water_mask_codes"

registry = register_source_registry(LayerRegistry("gfm"))

# ── Native inventory ─────────────────────────────────────────────────────
registry.add_native(
    NativeLayer(
        name="ensemble_flood_extent",
        dtype="uint8",
        nodata=GFM_NODATA,
        description="Ensemble SAR flood extent, passed through untouched.",
        codes={GFM_DRY: "dry / observed-not-flooded", GFM_FLOOD: "flood", GFM_NODATA: "nodata"},
        resampling="nearest",
        aggregation="masked_max",
    )
)
registry.add_native(
    NativeLayer(
        name="ensemble_water_extent",
        dtype="uint8",
        nodata=GFM_NODATA,
        description="Ensemble SAR water extent, passed through untouched.",
        codes={GFM_DRY: "dry / observed-not-water", GFM_WATER: "water", GFM_NODATA: "nodata"},
        resampling="nearest",
        aggregation="masked_max",
    )
)
registry.add_native(
    NativeLayer(
        name="reference_water_mask",
        dtype="uint8",
        nodata=GFM_NODATA,
        description=(
            "Reference water mask, passed through untouched. Codes follow GFM PDD "
            "Table 20: 0 = no water, 1 = permanent water, 2 = seasonal water. The "
            "seasonal class (2) is the GFM analog of MODIS recurring_flood."
        ),
        codes={
            GFM_LAND: "no water",
            GFM_PERMANENT_WATER: "permanent water",
            GFM_SEASONAL_WATER: "seasonal water",
            GFM_NODATA: "nodata",
        },
        resampling="nearest",
        aggregation="masked_max",
    )
)
registry.add_native(
    NativeLayer(
        name="exclusion_mask",
        dtype="uint8",
        nodata=GFM_NODATA,
        description="Native GFM exclusion-mask codes, passed through untouched.",
        resampling="nearest",
        aggregation="masked_max",
    )
)
registry.add_native(
    NativeLayer(
        name="ensemble_likelihood",
        dtype="uint8",
        nodata=GFM_NODATA,
        description="Native GFM ensemble flood-likelihood values (0-100), passed through untouched.",
        resampling="average",
        aggregation="masked_max",
    )
)
registry.add_native(
    NativeLayer(
        name="advisory_flags",
        dtype="uint8",
        nodata=GFM_NODATA,
        description="Native GFM advisory bitmask codes, passed through untouched.",
        resampling="nearest",
        aggregation="masked_or",
    )
)


# Importing the derived module registers GFM derivations on ``registry``.
# Kept at the end so ``registry`` and the code constants are fully defined first.
from atlantis.fetchers.gfm import derived as _derived  # noqa: E402,F401
