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

from collections.abc import Iterable

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
# Codes 16 (bareland) and 17 (vegetation) are land-cover classes, not confirmed
# dry-land observations. Flood pixels can be misclassified into either class
# (confirmed by the VIIRS product team, 2026-07), so both are treated as
# low-confidence/exclusion classes rather than usable "no flood" observations.
BARELAND_CODES = {16}
VEGETATION_CODES = {17}

# ── Configurable exclusion (invalid mask) ─────────────────────────────────
# `water_fraction` / `flood_fraction` / `exclusion_mask` all derive from the
# same "which codes count as invalid/excluded" decision. This is exposed as
# named categories (rather than raw codes) so a developer can, e.g., stop
# excluding vegetation/bareland without needing to know their numeric codes.
# Configure via `FetcherConfig.viirs_excluded_categories` /
# `viirs_exclude_extra_codes` (env: `ATLANTIS_VIIRS_EXCLUDED_CATEGORIES` /
# `ATLANTIS_VIIRS_EXCLUDE_EXTRA_CODES`), or per-run via `atlantis fetch
# --viirs-exclude-categories` / `--viirs-exclude-codes`.
EXCLUSION_CATEGORY_CODES: dict[str, frozenset[int]] = {
    "fill": frozenset(FILL_CODES),
    "cloud": frozenset(CLOUD_CODES),
    "snow_ice": frozenset(SNOW_ICE_CODES),
    "shadow": frozenset(SHADOW_CODES),
    "bareland": frozenset(BARELAND_CODES),
    "vegetation": frozenset(VEGETATION_CODES),
}

#: Categories excluded by default — matches Atlantis's historical behaviour.
DEFAULT_EXCLUDED_CATEGORIES: tuple[str, ...] = (
    "fill",
    "cloud",
    "snow_ice",
    "shadow",
    "bareland",
    "vegetation",
)


def resolve_excluded_codes(
    categories: Iterable[str] = DEFAULT_EXCLUDED_CATEGORIES,
    extra_codes: Iterable[int] = (),
) -> frozenset[int]:
    """Resolve the set of VIIRS pixel codes treated as invalid/excluded.

    Args:
        categories: Named groups to exclude, from :data:`EXCLUSION_CATEGORY_CODES`
            (``fill``, ``cloud``, ``snow_ice``, ``shadow``, ``bareland``,
            ``vegetation``). Defaults to all six (current/historical behaviour).
            Drop a name (e.g. omit ``"bareland"``/``"vegetation"``) to stop
            treating that category as invalid.
        extra_codes: Additional raw pixel codes to exclude regardless of
            category — an escape hatch for one-off codes not covered by a
            named category (e.g. 27 "river/lake ice", 38 "mixed snow/ice/water").

    Returns:
        Frozen set of excluded pixel codes.

    Raises:
        ValueError: if an unrecognised category name is given.
    """
    codes: set[int] = set()
    for name in categories:
        try:
            codes |= EXCLUSION_CATEGORY_CODES[name]
        except KeyError:
            valid = ", ".join(sorted(EXCLUSION_CATEGORY_CODES))
            raise ValueError(f"Unknown VIIRS exclusion category '{name}'. Expected one of: {valid}") from None
    codes |= set(extra_codes)
    return frozenset(codes)


def parse_category_list(value: str) -> list[str]:
    """Parse a comma-separated category-name string, trimming blanks/empties."""
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_code_list(value: str) -> list[int]:
    """Parse a comma-separated integer pixel-code string, trimming blanks/empties."""
    return [int(item.strip()) for item in value.split(",") if item.strip()]


#: Codes excluded with the default category set — Atlantis's historical
#: ``_invalid_mask`` behaviour, used whenever no override is supplied.
DEFAULT_EXCLUDED_CODES: frozenset[int] = resolve_excluded_codes()

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
