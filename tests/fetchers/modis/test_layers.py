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


def test_quality_mask_is_valid_where_not_insufficient() -> None:
    registry = get_source_registry("modis")
    out = registry.get_derived("quality_mask").derive(_ctx([[0, 3, 255]]))
    np.testing.assert_array_equal(out, np.array([[1, 1, 0]], dtype="uint8"))


def test_permanent_water_is_surface_water_code() -> None:
    registry = get_source_registry("modis")
    out = registry.get_derived("permanent_water").derive(_ctx([[0, 1, 2, 3]]))
    np.testing.assert_array_equal(out, np.array([[0, 1, 0, 0]], dtype="uint8"))


def test_recurring_flood_is_class_two() -> None:
    registry = get_source_registry("modis")
    out = registry.get_derived("recurring_flood").derive(_ctx([[0, 1, 2, 3]]))
    np.testing.assert_array_equal(out, np.array([[0, 0, 1, 0]], dtype="uint8"))


def test_derived_specs_carry_harmoniser_metadata() -> None:
    registry = get_source_registry("modis")
    flood = registry.get_derived("flood_fraction")
    assert flood.resampling == "average"
    assert flood.aggregation == "nanmean"
    assert flood.dtype == "float32"
    mask = registry.get_derived("quality_mask")
    assert mask.resampling == "mode"
