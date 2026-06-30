"""MODIS MCDWD native layer catalogue.

This module is the single source of truth for *what MODIS layers exist*. The
derived-layer definitions live in :mod:`atlantis.fetchers.modis.derived` and are
imported at the end of this module so a single ``import ...modis.layers``
populates the full registry (native + derived).

Derived layers (``flood_fraction``, ``quality_mask``, ``permanent_water``,
``recurring_flood``) are computed from the *selected* flood composite, which the
processor exposes to derivations under the input key :data:`SELECTED_COMPOSITE`.
"""

from __future__ import annotations

from atlantis.layers import (
    LayerRegistry,
    NativeLayer,
    register_source_registry,
)

# ── MCDWD pixel codes (Release 1.1, Dec 2025) ────────────────────────────
NO_WATER_CODE = 0
SURFACE_WATER_CODE = 1
RECURRING_FLOOD_CODE = 2
UNUSUAL_FLOOD_CODE = 3
INSUFFICIENT_DATA_CODE = 255

#: Derivation input key for the user-selected MCDWD flood composite (the
#: untouched ``0/1/2/3/255`` codes of whichever ``--composite`` was chosen).
SELECTED_COMPOSITE = "raw"

#: Pixel-code meanings shared by every MCDWD flood composite band.
FLOOD_COMPOSITE_CODES = {
    NO_WATER_CODE: "no water",
    SURFACE_WATER_CODE: "surface (reference) water",
    RECURRING_FLOOD_CODE: "recurring flood",
    UNUSUAL_FLOOD_CODE: "unusual flood",
    INSUFFICIENT_DATA_CODE: "insufficient data / masked",
}

registry = register_source_registry(LayerRegistry("modis"))

# ── Native inventory ─────────────────────────────────────────────────────
# The selected flood composite, passed through untouched. This is both the raw
# output band and the input every MODIS derivation reads.
registry.add_native(
    NativeLayer(
        name=SELECTED_COMPOSITE,
        dtype="uint8",
        nodata=INSUFFICIENT_DATA_CODE,
        description=(
            "Selected MCDWD flood composite codes, passed through untouched. "
            "Resolves via --composite to one of F1 (Flood_1Day_250m), "
            "F1C (FloodCS_1Day_250m), F2 (Flood_2Day_250m), or F3 (Flood_3Day_250m)."
        ),
        codes=FLOOD_COMPOSITE_CODES,
        resampling="nearest",
        aggregation="mode",
    )
)

# Observation-count layers shipped in every MCDWD HDF. Catalogued for discovery;
# not loaded by the default pipeline yet. Counts are clear-observation tallies
# (0-254) used to reason about confidence / cloud obstruction.
_COUNT_LAYERS: tuple[tuple[str, str], ...] = (
    ("TotalCounts_1Day_250m", "Potential observations over the 1-day window."),
    ("TotalCounts_2Day_250m", "Potential observations over the 2-day window."),
    ("TotalCounts_3Day_250m", "Potential observations over the 3-day window."),
    ("ValidCountsCS_1Day_250m", "Clear-sky observations (cloud-shadow screened), 1-day."),
    ("ValidCounts_1Day_250m", "Clear-sky observations, 1-day."),
    ("ValidCounts_2Day_250m", "Clear-sky observations, 2-day."),
    ("ValidCounts_3Day_250m", "Clear-sky observations, 3-day."),
    ("WaterCountsCS_1Day_250m", "Water detections (terrain + cloud-shadow masked), 1-day."),
    ("WaterCounts_1Day_250m", "Water detections (terrain masked), 1-day."),
    ("WaterCounts_2Day_250m", "Water detections, 2-day."),
    ("WaterCounts_3Day_250m", "Water detections, 3-day."),
)
for _name, _desc in _COUNT_LAYERS:
    registry.add_native(
        NativeLayer(
            name=_name,
            dtype="uint8",
            nodata=INSUFFICIENT_DATA_CODE,
            description=f"{_desc} Catalogued upstream layer; not loaded by the default pipeline.",
            resampling="average",
            aggregation="mean",
        )
    )


# Importing the derived module registers MODIS derivations on ``registry``.
# Kept at the end so ``registry`` and the code constants are fully defined first.
from atlantis.fetchers.modis import derived as _derived  # noqa: E402,F401
