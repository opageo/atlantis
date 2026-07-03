"""Tests for the VIIRS layer registry (native manifest + derived layers)."""

from __future__ import annotations

import numpy as np

from atlantis.layers import DerivationContext, get_source_registry


def _ctx(codes: list[list[int]]) -> DerivationContext:
    return DerivationContext(arrays={"raw": np.array(codes, dtype="uint8")})


def test_native_manifest_lists_single_band_with_codes() -> None:
    registry = get_source_registry("viirs")
    native = registry.list_native()
    assert [layer.name for layer in native] == ["raw"]
    assert native[0].codes is not None
    # Transcribed from the embedded NOAA WaterDetection#TypeDescription tag.
    assert native[0].codes[20] == "snow_ice"
    assert native[0].codes[99] == "normal water (NOAA reference)"
    assert native[0].nodata == 1


def test_flood_fraction_decodes_water_fraction_codes() -> None:
    registry = get_source_registry("viirs")
    # 100 -> 0.0 (boundary, not a flood code), 150 -> 0.5, 200 -> 1.0.
    out = registry.get_derived("flood_fraction").derive(_ctx([[100, 150, 200]]))
    np.testing.assert_allclose(out, np.array([[0.0, 0.5, 1.0]], dtype="float32"))


def test_flood_fraction_nan_for_fill_and_cloud() -> None:
    registry = get_source_registry("viirs")
    out = registry.get_derived("flood_fraction").derive(_ctx([[0, 1, 30, 17]]))
    assert np.isnan(out[0, 0]) and np.isnan(out[0, 1]) and np.isnan(out[0, 2])
    assert out[0, 3] == 0.0


def test_quality_mask_invalidates_fill_and_cloud() -> None:
    registry = get_source_registry("viirs")
    out = registry.get_derived("quality_mask").derive(_ctx([[0, 1, 30, 17, 99]]))
    np.testing.assert_array_equal(out, np.array([[0, 0, 0, 1, 1]], dtype="uint8"))


def test_permanent_water_is_code_99() -> None:
    registry = get_source_registry("viirs")
    out = registry.get_derived("permanent_water").derive(_ctx([[99, 20, 17, 0]]))
    np.testing.assert_array_equal(out, np.array([[1, 0, 0, 0]], dtype="uint8"))


def test_cloud_mask_snow_ice_and_shadow() -> None:
    registry = get_source_registry("viirs")
    codes = _ctx([[30, 20, 50, 17]])
    np.testing.assert_array_equal(
        registry.get_derived("cloud_mask").derive(codes), np.array([[1, 0, 0, 0]], dtype="uint8")
    )
    np.testing.assert_array_equal(
        registry.get_derived("snow_ice").derive(codes), np.array([[0, 1, 0, 0]], dtype="uint8")
    )
    np.testing.assert_array_equal(registry.get_derived("shadow").derive(codes), np.array([[0, 0, 1, 0]], dtype="uint8"))


def test_classify_routes_new_layers_to_extra_layers() -> None:
    from rasterio.transform import from_origin

    from atlantis.fetchers.viirs.processor import classify_viirs_pixels

    data = np.array([[0, 30, 20, 50], [150, 99, 17, 16]], dtype="uint8")
    processed = classify_viirs_pixels(data, from_origin(0, 2, 1, 1), "EPSG:4326")
    # Core layers stay on named fields; new layers go to extra_layers.
    assert set(processed.extra_layers) == {"cloud_mask", "snow_ice", "shadow"}
    assert processed.flood_fraction is not None
