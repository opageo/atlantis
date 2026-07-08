"""Sync tests: registry-declared aggregation must match what processors apply.

Each source's ``aggregate_tiles`` is treated as a black box. For every layer it
reduces, we build synthetic tiles, run the processor, and assert that the
output equals the result of calling :func:`~atlantis.layers.aggregate_layer`
with the operator and nodata value declared in the registry.
"""

from __future__ import annotations

import numpy as np
from rasterio.transform import from_bounds

from atlantis.fetchers.gfm.processor import GfmProcessedTile, GfmRasterProcessor
from atlantis.fetchers.modis.processor import (
    ModisRasterProcessor,
)
from atlantis.fetchers.modis.processor import (
    ProcessedTile as ModisProcessedTile,
)
from atlantis.fetchers.viirs.processor import (
    ProcessedTile as ViirsProcessedTile,
)
from atlantis.fetchers.viirs.processor import (
    ViirsRasterProcessor,
)
from atlantis.layers import aggregate_layer, get_source_registry


def _expected(stack: np.ndarray, spec, *, valid_stack: np.ndarray | None = None) -> np.ndarray:
    """Reduce *stack* exactly as the registry says it should be reduced."""
    return aggregate_layer(
        stack,
        spec.aggregation,  # type: ignore[arg-type]
        nodata=spec.nodata,
        valid_stack=valid_stack if spec.aggregation == "majority" else None,
    )


class TestGfmAggregationSync:
    """GFM aggregate_tiles must follow the registry for every layer."""

    def _make_classified_tiles(self) -> list[GfmProcessedTile]:
        transform = from_bounds(0, 0, 1, 1, 3, 3)
        return [
            GfmProcessedTile(
                water_fraction=np.array([[0.2, 0.4, np.nan]], dtype=np.float32),
                flood_fraction=np.array([[0.1, 0.5, np.nan]], dtype=np.float32),
                reference_water=np.array([[0, 1, 255]], dtype=np.uint8),
                extra_layers={
                    "exclusion_mask": np.array([[0, 0, 1]], dtype=np.uint8),
                    "advisory_flags": np.array([[1, 2, 4]], dtype=np.uint8),
                    "ensemble_likelihood": np.array([[10, 20, 255]], dtype=np.uint8),
                },
                transform=transform,
                crs="EPSG:4326",
                shape=(1, 3),
            ),
            GfmProcessedTile(
                water_fraction=np.array([[0.8, 0.6, 0.0]], dtype=np.float32),
                flood_fraction=np.array([[0.9, 0.3, 0.0]], dtype=np.float32),
                reference_water=np.array([[2, 0, 1]], dtype=np.uint8),
                extra_layers={
                    "exclusion_mask": np.array([[0, 1, 0]], dtype=np.uint8),
                    "advisory_flags": np.array([[2, 1, 8]], dtype=np.uint8),
                    "ensemble_likelihood": np.array([[5, 255, 30]], dtype=np.uint8),
                },
                transform=transform,
                crs="EPSG:4326",
                shape=(1, 3),
            ),
        ]

    def _make_native_tiles(self) -> list[GfmProcessedTile]:
        transform = from_bounds(0, 0, 1, 1, 3, 3)
        return [
            GfmProcessedTile(
                ensemble_flood_extent=np.array([[0, 1, 255]], dtype=np.uint8),
                reference_water_mask=np.array([[0, 1, 255]], dtype=np.uint8),
                extra_layers={
                    "ensemble_water_extent": np.array([[1, 0, 255]], dtype=np.uint8),
                    "exclusion_mask": np.array([[0, 255, 1]], dtype=np.uint8),
                    "advisory_flags": np.array([[1, 2, 255]], dtype=np.uint8),
                    "ensemble_likelihood": np.array([[10, 255, 20]], dtype=np.uint8),
                },
                transform=transform,
                crs="EPSG:4326",
                shape=(1, 3),
            ),
            GfmProcessedTile(
                ensemble_flood_extent=np.array([[1, 0, 0]], dtype=np.uint8),
                reference_water_mask=np.array([[2, 255, 1]], dtype=np.uint8),
                extra_layers={
                    "ensemble_water_extent": np.array([[0, 1, 0]], dtype=np.uint8),
                    "exclusion_mask": np.array([[255, 0, 0]], dtype=np.uint8),
                    "advisory_flags": np.array([[2, 4, 1]], dtype=np.uint8),
                    "ensemble_likelihood": np.array([[255, 5, 30]], dtype=np.uint8),
                },
                transform=transform,
                crs="EPSG:4326",
                shape=(1, 3),
            ),
        ]

    def test_classified_mode_matches_registry(self):
        registry = get_source_registry("gfm")
        tiles = self._make_classified_tiles()
        result = GfmRasterProcessor.aggregate_tiles(tiles)
        assert result is not None
        assert result.ensemble_flood_extent is None

        for name, field in (
            ("water_fraction", "water_fraction"),
            ("flood_fraction", "flood_fraction"),
            ("reference_water", "reference_water"),
        ):
            stack = np.stack([getattr(t, field) for t in tiles], axis=0)
            expected = _expected(stack, registry.get(name))
            np.testing.assert_allclose(getattr(result, field), expected, rtol=1e-6)

        for name in tiles[0].extra_layers:
            stack = np.stack([t.extra_layers[name] for t in tiles], axis=0)
            expected = _expected(stack, registry.get(name))
            np.testing.assert_array_equal(result.extra_layers[name], expected)

    def test_native_mode_matches_registry(self):
        registry = get_source_registry("gfm")
        tiles = self._make_native_tiles()
        result = GfmRasterProcessor.aggregate_tiles(tiles)
        assert result is not None
        assert result.water_fraction is None

        for name, field in (
            ("ensemble_flood_extent", "ensemble_flood_extent"),
            ("reference_water_mask", "reference_water_mask"),
        ):
            stack = np.stack([getattr(t, field) for t in tiles], axis=0)
            expected = _expected(stack, registry.get(name))
            np.testing.assert_array_equal(getattr(result, field), expected)

        for name in tiles[0].extra_layers:
            stack = np.stack([t.extra_layers[name] for t in tiles], axis=0)
            expected = _expected(stack, registry.get(name))
            np.testing.assert_array_equal(result.extra_layers[name], expected)


class TestModisAggregationSync:
    """MODIS aggregate_tiles must follow the registry for every layer."""

    def _make_tiles(self) -> list[ModisProcessedTile]:
        transform = from_bounds(0, 0, 1, 1, 3, 3)
        return [
            ModisProcessedTile(
                raw=np.array([[0, 1, 255]], dtype=np.uint8),
                water_fraction=np.array([[0.2, 0.4, np.nan]], dtype=np.float32),
                flood_fraction=np.array([[0.1, 0.5, np.nan]], dtype=np.float32),
                exclusion_mask=np.array([[0, 0, 1]], dtype=np.uint8),
                reference_water=np.array([[0, 1, 0]], dtype=np.uint8),
                recurring_flood=np.array([[0, 0, 1]], dtype=np.uint8),
                transform=transform,
                crs="EPSG:4326",
                cloud_fraction=0.1,
            ),
            ModisProcessedTile(
                raw=np.array([[3, 2, 1]], dtype=np.uint8),
                water_fraction=np.array([[0.8, 0.6, 0.0]], dtype=np.float32),
                flood_fraction=np.array([[0.9, 0.3, 0.0]], dtype=np.float32),
                exclusion_mask=np.array([[0, 1, 0]], dtype=np.uint8),
                reference_water=np.array([[1, 1, 0]], dtype=np.uint8),
                recurring_flood=np.array([[0, 1, 0]], dtype=np.uint8),
                transform=transform,
                crs="EPSG:4326",
                cloud_fraction=0.3,
            ),
        ]

    def test_all_layers_match_registry(self):
        registry = get_source_registry("modis")
        tiles = self._make_tiles()
        result = ModisRasterProcessor.aggregate_tiles(tiles)

        for name, field in (
            ("raw", "raw"),
            ("water_fraction", "water_fraction"),
            ("flood_fraction", "flood_fraction"),
            ("exclusion_mask", "exclusion_mask"),
            ("reference_water", "reference_water"),
            ("recurring_flood", "recurring_flood"),
        ):
            stack = np.stack([getattr(t, field) for t in tiles], axis=0)
            expected = _expected(stack, registry.get(name))
            if field in ("water_fraction", "flood_fraction"):
                np.testing.assert_allclose(getattr(result, field), expected, rtol=1e-6)
            else:
                np.testing.assert_array_equal(getattr(result, field), expected)


class TestViirsAggregationSync:
    """VIIRS aggregate_tiles must follow the registry for every layer."""

    def _make_tiles(self) -> list[ViirsProcessedTile]:
        transform = from_bounds(0, 0, 1, 1, 3, 3)
        return [
            ViirsProcessedTile(
                raw=np.array([[100, 99, 1]], dtype=np.uint8),
                water_fraction=np.array([[0.0, 1.0, np.nan]], dtype=np.float32),
                flood_fraction=np.array([[0.0, 0.0, np.nan]], dtype=np.float32),
                exclusion_mask=np.array([[0, 0, 1]], dtype=np.uint8),
                reference_water=np.array([[0, 1, 0]], dtype=np.uint8),
                extra_layers={
                    "cloud_mask": np.array([[0, 0, 1]], dtype=np.uint8),
                    "snow_ice": np.array([[0, 0, 0]], dtype=np.uint8),
                    "shadow": np.array([[0, 1, 0]], dtype=np.uint8),
                },
                transform=transform,
                crs="EPSG:4326",
                cloud_fraction=0.1,
            ),
            ViirsProcessedTile(
                raw=np.array([[150, 99, 30]], dtype=np.uint8),
                water_fraction=np.array([[0.5, 1.0, 0.0]], dtype=np.float32),
                flood_fraction=np.array([[0.5, 0.0, 0.0]], dtype=np.float32),
                exclusion_mask=np.array([[0, 1, 1]], dtype=np.uint8),
                reference_water=np.array([[0, 1, 0]], dtype=np.uint8),
                extra_layers={
                    "cloud_mask": np.array([[0, 1, 1]], dtype=np.uint8),
                    "snow_ice": np.array([[0, 0, 1]], dtype=np.uint8),
                    "shadow": np.array([[1, 0, 0]], dtype=np.uint8),
                },
                transform=transform,
                crs="EPSG:4326",
                cloud_fraction=0.2,
            ),
        ]

    def test_all_layers_match_registry(self):
        registry = get_source_registry("viirs")
        tiles = self._make_tiles()
        result = ViirsRasterProcessor.aggregate_tiles(tiles)

        for name, field in (
            ("raw", "raw"),
            ("water_fraction", "water_fraction"),
            ("flood_fraction", "flood_fraction"),
            ("exclusion_mask", "exclusion_mask"),
            ("reference_water", "reference_water"),
        ):
            stack = np.stack([getattr(t, field) for t in tiles], axis=0)
            spec = registry.get(name)
            valid_stack = None
            if spec.aggregation == "majority":
                em_stack = np.stack([t.exclusion_mask for t in tiles], axis=0)
                valid_stack = ~(em_stack > 0)
            expected = _expected(stack, spec, valid_stack=valid_stack)
            if field in ("water_fraction", "flood_fraction"):
                np.testing.assert_allclose(getattr(result, field), expected, rtol=1e-6)
            else:
                np.testing.assert_array_equal(getattr(result, field), expected)

        for name in tiles[0].extra_layers:
            stack = np.stack([t.extra_layers[name] for t in tiles], axis=0)
            expected = _expected(stack, registry.get(name))
            np.testing.assert_array_equal(result.extra_layers[name], expected)
