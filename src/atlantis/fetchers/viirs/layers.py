"""VIIRS flood-product native layer catalogue.

Single source of truth for *what VIIRS layers exist*. VIIRS ships a single
encoded raster band whose integer codes pack land-cover, cloud, water, and
flood-fraction classes together. The derived-layer definitions live in
:mod:`atlantis.fetchers.viirs.derived` and are imported at the end of this
module so a single ``import ...viirs.layers`` populates the full registry.

Derived layers (``water_fraction``, ``flood_fraction``, ``reference_water``,
``exclusion_mask``) are computed from that single band, which the processor
exposes to derivations under the input key :data:`SELECTED_BAND`.
"""

from __future__ import annotations

from atlantis.layers import (
    LayerRegistry,
    NativeLayer,
    register_source_registry,
)

# ── VIIRS source code table ──────────────────────────────────────────────
FLOOD_MIN_CODE = 160  #: conservative default: ≥60% water fraction
FILL_CODES = {0, 1}
CLOUD_CODES = {30}
# Code 99 ("NormalWater" per the embedded NOAA TIFF metadata tag
# ``WaterDetection#TypeDescription``) is the VIIRS reference-water class — what
# Atlantis exposes as ``permanent_water``. Code 17 is Vegetation, not water.
PERMANENT_WATER_CODES = {99}
# Code 20 is the NOAA "Snow / ice" class (per WaterDetection#TypeDescription).
SNOW_ICE_CODES = {20}
#: Deprecated alias kept for backwards compatibility; code 20 is snow/ice.
SEASONAL_WATER_CODES = SNOW_ICE_CODES
OPEN_WATER_CODES = {99}
SHADOW_CODES = {50}
CLASSIFIED_FLOOD_NODATA = 255

#: Derivation input key for the single encoded VIIRS band (untouched codes).
SELECTED_BAND = "raw"

#: Pixel-code meanings transcribed verbatim from the embedded NOAA band tag
#: ``WaterDetection#TypeDescription`` of the raw VFM GeoTIFF (the authoritative
#: legend). The source ``_FillValue`` is ``1``; code ``0`` does not occur in the
#: source — Atlantis additionally treats ``0`` (clip / mosaic fill) as missing.
#: Codes 100-200 encode water fraction as ``(code - 100)%``.
VIIRS_BAND_CODES = {
    1: "no_valid_data (source fill)",
    15: "floodwater without fraction retrieval",
    16: "bareland",
    17: "vegetation",
    20: "snow_ice",
    27: "river/lake ice",
    30: "cloud",
    38: "super-snow/ice water or mixed ice & water or melting ice",
    50: "shadow",
    99: "normal water (NOAA reference)",
}

registry = register_source_registry(LayerRegistry("viirs"))

# ── Native inventory ─────────────────────────────────────────────────────
registry.add_native(
    NativeLayer(
        name=SELECTED_BAND,
        dtype="uint8",
        nodata=1,
        description=(
            "Single encoded VIIRS flood band (NOAA VFM), passed through untouched. "
            "Codes 100-200 encode water fraction as (code - 100)%; other codes are "
            "land-cover, cloud, or water classes (see codes). The source _FillValue "
            "is 1 (no_valid_data); Atlantis also treats 0 (clip/mosaic fill) as missing "
            "and writes its raw GeoTIFF with nodata=0."
        ),
        codes=VIIRS_BAND_CODES,
        resampling="nearest",
        aggregation="mode",
    )
)


# Importing the derived module registers VIIRS derivations on ``registry``.
# Kept at the end so ``registry`` and the code constants are fully defined first.
from atlantis.fetchers.viirs import derived as _derived  # noqa: E402,F401
