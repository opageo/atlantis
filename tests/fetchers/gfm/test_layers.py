"""Tests for the GFM layer registry (native manifest + count-based derived layers)."""

from __future__ import annotations

import numpy as np

from atlantis.layers import DerivationContext, get_source_registry


def _ctx(flood: np.ndarray, perm: np.ndarray, valid: np.ndarray) -> DerivationContext:
    return DerivationContext(
        arrays={
            "flood_count": flood.astype("float32"),
            "perm_water_count": perm.astype("float32"),
            "valid_count": valid.astype("float32"),
        }
    )


def test_native_manifest_lists_two_sar_bands() -> None:
    registry = get_source_registry("gfm")
    names = [layer.name for layer in registry.list_native()]
    assert names == ["ensemble_flood_extent", "reference_water_mask"]


def test_flood_fraction_is_flood_over_valid_with_nan_for_unobserved() -> None:
    registry = get_source_registry("gfm")
    flood = np.array([[2.0, 0.0]])
    valid = np.array([[4.0, 0.0]])
    perm = np.zeros((1, 2))
    out = registry.get_derived("flood_fraction").derive(_ctx(flood, perm, valid))
    assert out[0, 0] == 0.5
    assert np.isnan(out[0, 1])


def test_quality_mask_is_any_valid_observation() -> None:
    registry = get_source_registry("gfm")
    valid = np.array([[0.0, 1.0, 3.0]])
    out = registry.get_derived("quality_mask").derive(_ctx(np.zeros((1, 3)), np.zeros((1, 3)), valid))
    np.testing.assert_array_equal(out, np.array([[0, 1, 1]], dtype="uint8"))


def test_permanent_water_needs_majority_coverage() -> None:
    registry = get_source_registry("gfm")
    perm = np.array([[3.0, 1.0]])
    valid = np.array([[4.0, 4.0]])
    out = registry.get_derived("permanent_water").derive(_ctx(np.zeros((1, 2)), perm, valid))
    np.testing.assert_array_equal(out, np.array([[1, 0]], dtype="uint8"))


def test_native_bands_use_nearest_and_max() -> None:
    registry = get_source_registry("gfm")
    efe = registry.get_native("ensemble_flood_extent")
    assert efe.resampling == "nearest"
    assert efe.aggregation == "max"
    assert efe.codes[1] == "flood"
