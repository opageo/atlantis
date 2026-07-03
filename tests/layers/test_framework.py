"""Tests for the source-agnostic layer framework."""

from __future__ import annotations

import numpy as np
import pytest

from atlantis.layers import (
    DerivationContext,
    DerivedLayer,
    LayerKind,
    LayerRegistry,
    NativeLayer,
)


def _make_registry() -> LayerRegistry:
    registry = LayerRegistry("demo")
    registry.add_native(
        NativeLayer(
            name="flood_composite",
            dtype="uint8",
            nodata=255,
            description="Selected flood composite",
            codes={3: "unusual flood"},
        )
    )

    @registry.derived(
        name="flood_fraction",
        inputs=("flood_composite",),
        dtype="float32",
        nodata=None,
        resampling="average",
        aggregation="nanmean",
        description="Binary class==3 flag, NaN for nodata.",
    )
    def flood_fraction(ctx: DerivationContext) -> np.ndarray:
        arr = ctx["flood_composite"]
        out = (arr == 3).astype("float32")
        out[arr == 255] = np.nan
        return out

    return registry


def test_native_and_derived_kinds() -> None:
    registry = _make_registry()
    assert registry.get_native("flood_composite").kind is LayerKind.NATIVE
    assert registry.get_derived("flood_fraction").kind is LayerKind.DERIVED


def test_listing_is_native_then_derived_in_order() -> None:
    registry = _make_registry()
    assert [layer.name for layer in registry.list_native()] == ["flood_composite"]
    assert [layer.name for layer in registry.list_derived()] == ["flood_fraction"]
    assert registry.names() == ["flood_composite", "flood_fraction"]


def test_derive_is_pure_and_callable() -> None:
    registry = _make_registry()
    derived = registry.get_derived("flood_fraction")
    assert isinstance(derived, DerivedLayer)
    ctx = DerivationContext(arrays={"flood_composite": np.array([[0, 3, 255], [1, 2, 3]], dtype="uint8")})
    out = derived.derive(ctx)
    expected = np.array([[0.0, 1.0, np.nan], [0.0, 0.0, 1.0]], dtype="float32")
    np.testing.assert_array_equal(out, expected)


def test_duplicate_registration_raises() -> None:
    registry = _make_registry()
    with pytest.raises(ValueError, match="already registered"):
        registry.add_native(NativeLayer(name="flood_composite", dtype="uint8", nodata=255, description="dup"))


def test_missing_input_raises_with_available_names() -> None:
    ctx = DerivationContext(arrays={"a": np.zeros((1, 1))})
    with pytest.raises(KeyError, match="not available"):
        _ = ctx["missing"]
    assert "a" in ctx


def test_get_unknown_layer_raises() -> None:
    registry = _make_registry()
    with pytest.raises(KeyError, match="no layer"):
        registry.get("nope")


def test_contains_and_iter() -> None:
    registry = _make_registry()
    assert "flood_composite" in registry
    assert "flood_fraction" in registry
    assert [layer.name for layer in registry] == ["flood_composite", "flood_fraction"]
