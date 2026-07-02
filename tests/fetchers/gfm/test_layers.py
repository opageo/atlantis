"""Tests for the GFM layer registry (native manifest + count-based derived layers)."""

from __future__ import annotations

import numpy as np

from atlantis.layers import DerivationContext, get_source_registry


def _ctx(
    flood: np.ndarray,
    water: np.ndarray,
    valid: np.ndarray,
    reference: np.ndarray | None = None,
) -> DerivationContext:
    ref = reference if reference is not None else np.full(flood.shape, 255, dtype=np.uint8)
    return DerivationContext(
        arrays={
            "flood_count": flood.astype("float32"),
            "water_count": water.astype("float32"),
            "valid_count": valid.astype("float32"),
            "reference_water_codes": ref.astype("uint8"),
        }
    )


def test_native_manifest_lists_full_gfm_asset_surface() -> None:
    registry = get_source_registry("gfm")
    names = [layer.name for layer in registry.list_native()]
    assert names == [
        "ensemble_flood_extent",
        "ensemble_water_extent",
        "reference_water_mask",
        "exclusion_mask",
        "ensemble_likelihood",
        "advisory_flags",
    ]


def test_water_fraction_is_water_over_valid_with_nan_for_unobserved() -> None:
    registry = get_source_registry("gfm")
    water = np.array([[3.0, 0.0]])
    valid = np.array([[4.0, 0.0]])
    flood = np.zeros((1, 2))
    out = registry.get_derived("water_fraction").derive(_ctx(flood, water, valid))
    assert out[0, 0] == 0.75
    assert np.isnan(out[0, 1])


def test_flood_fraction_is_flood_over_valid_with_nan_for_unobserved() -> None:
    registry = get_source_registry("gfm")
    flood = np.array([[2.0, 0.0]])
    valid = np.array([[4.0, 0.0]])
    water = np.zeros((1, 2))
    out = registry.get_derived("flood_fraction").derive(_ctx(flood, water, valid))
    assert out[0, 0] == 0.5
    assert np.isnan(out[0, 1])


def test_reference_water_is_passed_through_under_shared_name() -> None:
    registry = get_source_registry("gfm")
    reference = np.array([[0, 1, 2, 255]], dtype=np.uint8)
    out = registry.get_derived("reference_water").derive(
        _ctx(np.zeros((1, 4)), np.zeros((1, 4)), np.ones((1, 4)), reference)
    )
    np.testing.assert_array_equal(out, reference)


def test_native_bands_use_nearest_and_max() -> None:
    registry = get_source_registry("gfm")
    efe = registry.get_native("ensemble_flood_extent")
    assert efe.resampling == "nearest"
    assert efe.aggregation == "max"
    assert efe.codes[1] == "flood"
    ewe = registry.get_native("ensemble_water_extent")
    assert ewe.codes[1] == "water"
