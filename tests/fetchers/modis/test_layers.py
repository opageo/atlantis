"""Tests for the MODIS layer registry (native manifest + derived layers)."""

from __future__ import annotations

import numpy as np

from atlantis.layers import DerivationContext, get_source_registry


def _ctx(codes: list[list[int]]) -> DerivationContext:
    return DerivationContext(arrays={"raw": np.array(codes, dtype="uint8")})


def test_native_manifest_lists_composite_and_counts() -> None:
    registry = get_source_registry("modis")
    names = [layer.name for layer in registry.list_native()]
    assert names[0] == "raw"
    assert "ValidCounts_2Day_250m" in names
    assert "WaterCountsCS_1Day_250m" in names
    # 1 selected composite + 11 count layers.
    assert len(names) == 12


def test_flood_fraction_marks_unusual_flood_and_nan_for_nodata() -> None:
    registry = get_source_registry("modis")
    out = registry.get_derived("flood_fraction").derive(_ctx([[0, 1, 2, 3, 255]]))
    expected = np.array([[0.0, 0.0, 0.0, 1.0, np.nan]], dtype="float32")
    np.testing.assert_array_equal(out, expected)


def test_water_fraction_marks_all_water_classes_and_nan_for_nodata() -> None:
    registry = get_source_registry("modis")
    out = registry.get_derived("water_fraction").derive(_ctx([[0, 1, 2, 3, 255]]))
    expected = np.array([[0.0, 1.0, 1.0, 1.0, np.nan]], dtype="float32")
    np.testing.assert_array_equal(out, expected)


def test_exclusion_mask_flags_only_insufficient_data() -> None:
    registry = get_source_registry("modis")
    out = registry.get_derived("exclusion_mask").derive(_ctx([[0, 3, 255]]))
    np.testing.assert_array_equal(out, np.array([[0, 0, 1]], dtype="uint8"))


def test_reference_water_includes_surface_and_recurring_classes() -> None:
    registry = get_source_registry("modis")
    out = registry.get_derived("reference_water").derive(_ctx([[0, 1, 2, 3]]))
    np.testing.assert_array_equal(out, np.array([[0, 1, 1, 0]], dtype="uint8"))


def test_recurring_flood_is_class_two() -> None:
    registry = get_source_registry("modis")
    out = registry.get_derived("recurring_flood").derive(_ctx([[0, 1, 2, 3]]))
    np.testing.assert_array_equal(out, np.array([[0, 0, 1, 0]], dtype="uint8"))


def test_derived_specs_carry_harmoniser_metadata() -> None:
    registry = get_source_registry("modis")
    water = registry.get_derived("water_fraction")
    assert water.resampling == "average"
    assert water.aggregation == "nanmean"
    assert water.dtype == "float32"
    flood = registry.get_derived("flood_fraction")
    assert flood.resampling == "average"
    assert flood.aggregation == "nanmean"
    assert flood.dtype == "float32"
    mask = registry.get_derived("exclusion_mask")
    assert mask.resampling == "mode"
