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


def test_water_fraction_promotes_reference_and_unquantified_water() -> None:
    registry = get_source_registry("viirs")
    # Code 17 (vegetation) is now excluded → NaN, not 0.0.
    out = registry.get_derived("water_fraction").derive(_ctx([[15, 99, 100, 150, 200, 17]]))
    expected = np.array([[1.0, 1.0, 0.0, 0.5, 1.0, np.nan]], dtype="float32")
    np.testing.assert_allclose(out[:, :5], expected[:, :5])
    assert np.isnan(out[0, 5])


def test_flood_fraction_nan_for_fill_and_cloud() -> None:
    registry = get_source_registry("viirs")
    # Code 17 (vegetation) is now excluded → NaN, not 0.0.
    out = registry.get_derived("flood_fraction").derive(_ctx([[0, 1, 30, 17]]))
    assert np.isnan(out[0, 0]) and np.isnan(out[0, 1]) and np.isnan(out[0, 2]) and np.isnan(out[0, 3])


def test_exclusion_mask_invalidates_fill_and_cloud() -> None:
    registry = get_source_registry("viirs")
    # Code 17 (vegetation) is now excluded too — see test_exclusion_mask_invalidates_vegetation_and_bareland.
    out = registry.get_derived("exclusion_mask").derive(_ctx([[0, 1, 30, 17, 99]]))
    np.testing.assert_array_equal(out, np.array([[1, 1, 1, 1, 0]], dtype="uint8"))


def test_exclusion_mask_invalidates_vegetation_and_bareland() -> None:
    registry = get_source_registry("viirs")
    # Codes 16 (bareland) and 17 (vegetation) are low-confidence land-cover
    # classes, not confirmed dry land — flood pixels can be misclassified
    # into either, so both must be excluded (per VIIRS product team guidance).
    out = registry.get_derived("exclusion_mask").derive(_ctx([[16, 17, 99, 160]]))
    np.testing.assert_array_equal(out, np.array([[1, 1, 0, 0]], dtype="uint8"))


def test_reference_water_is_code_99() -> None:
    registry = get_source_registry("viirs")
    out = registry.get_derived("reference_water").derive(_ctx([[99, 20, 17, 0]]))
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
    assert processed.water_fraction is not None
    assert processed.flood_fraction is not None
