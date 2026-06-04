"""Tests for the GFM raster processor."""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from rasterio.transform import from_bounds

from atlantis.fetchers.gfm.processor import (
    GFM_DRY,
    GFM_FLOOD,
    GFM_NODATA,
    GFM_PERMANENT_WATER,
    GfmProcessedTile,
    GfmRasterProcessor,
)


class TestGfmProcessorClassify:
    """Test the classification logic directly."""

    def test_all_flood(self):
        """All valid pixels are flood → flood_fraction = 1.0."""
        proc = GfmRasterProcessor(bbox=(0, 0, 1, 1))
        flood_count = np.full((5, 5), 10, dtype=np.uint32)
        perm_water_count = np.zeros((5, 5), dtype=np.uint32)
        valid_count = np.full((5, 5), 10, dtype=np.uint32)

        import xarray as xr

        coords = {"x": np.linspace(0, 1, 5), "y": np.linspace(1, 0, 5)}
        dims = ("y", "x")
        mock_coords = xr.DataArray(np.zeros((5, 5)), coords=coords, dims=dims).coords

        tile = proc._classify(flood_count, perm_water_count, valid_count, mock_coords, dims)

        assert tile.flood_fraction.shape == (5, 5)
        np.testing.assert_allclose(tile.flood_fraction, 1.0)
        np.testing.assert_array_equal(tile.quality_mask, 1)
        np.testing.assert_array_equal(tile.permanent_water, 0)

    def test_all_dry(self):
        """All valid pixels are dry → flood_fraction = 0.0."""
        proc = GfmRasterProcessor(bbox=(0, 0, 1, 1))
        flood_count = np.zeros((5, 5), dtype=np.uint32)
        perm_water_count = np.zeros((5, 5), dtype=np.uint32)
        valid_count = np.full((5, 5), 10, dtype=np.uint32)

        import xarray as xr

        coords = {"x": np.linspace(0, 1, 5), "y": np.linspace(1, 0, 5)}
        dims = ("y", "x")
        mock_coords = xr.DataArray(np.zeros((5, 5)), coords=coords, dims=dims).coords

        tile = proc._classify(flood_count, perm_water_count, valid_count, mock_coords, dims)

        np.testing.assert_allclose(tile.flood_fraction, 0.0)
        np.testing.assert_array_equal(tile.quality_mask, 1)

    def test_no_valid_data(self):
        """No valid observations → flood_fraction = NaN, quality_mask = 0."""
        proc = GfmRasterProcessor(bbox=(0, 0, 1, 1))
        flood_count = np.zeros((5, 5), dtype=np.uint32)
        perm_water_count = np.zeros((5, 5), dtype=np.uint32)
        valid_count = np.zeros((5, 5), dtype=np.uint32)

        import xarray as xr

        coords = {"x": np.linspace(0, 1, 5), "y": np.linspace(1, 0, 5)}
        dims = ("y", "x")
        mock_coords = xr.DataArray(np.zeros((5, 5)), coords=coords, dims=dims).coords

        tile = proc._classify(flood_count, perm_water_count, valid_count, mock_coords, dims)

        assert np.all(np.isnan(tile.flood_fraction))
        np.testing.assert_array_equal(tile.quality_mask, 0)
        assert tile.cloud_fraction == 1.0

    def test_mixed_data(self):
        """Mix of flood and dry → fractional values."""
        proc = GfmRasterProcessor(bbox=(0, 0, 1, 1))
        flood_count = np.array([[3, 0], [1, 5]], dtype=np.uint32)
        perm_water_count = np.zeros((2, 2), dtype=np.uint32)
        valid_count = np.array([[10, 10], [10, 10]], dtype=np.uint32)

        import xarray as xr

        coords = {"x": np.linspace(0, 1, 2), "y": np.linspace(1, 0, 2)}
        dims = ("y", "x")
        mock_coords = xr.DataArray(np.zeros((2, 2)), coords=coords, dims=dims).coords

        tile = proc._classify(flood_count, perm_water_count, valid_count, mock_coords, dims)

        np.testing.assert_allclose(tile.flood_fraction[0, 0], 0.3)
        np.testing.assert_allclose(tile.flood_fraction[0, 1], 0.0)
        np.testing.assert_allclose(tile.flood_fraction[1, 0], 0.1)
        np.testing.assert_allclose(tile.flood_fraction[1, 1], 0.5)

    def test_permanent_water_majority(self):
        """Permanent water majority vote: > 50% → permanent_water = 1."""
        proc = GfmRasterProcessor(bbox=(0, 0, 1, 1))
        flood_count = np.zeros((2, 2), dtype=np.uint32)
        perm_water_count = np.array([[6, 4], [8, 2]], dtype=np.uint32)
        valid_count = np.full((2, 2), 10, dtype=np.uint32)

        import xarray as xr

        coords = {"x": np.linspace(0, 1, 2), "y": np.linspace(1, 0, 2)}
        dims = ("y", "x")
        mock_coords = xr.DataArray(np.zeros((2, 2)), coords=coords, dims=dims).coords

        tile = proc._classify(flood_count, perm_water_count, valid_count, mock_coords, dims)

        assert tile.permanent_water[0, 0] == 1  # 6/10 > 0.5
        assert tile.permanent_water[0, 1] == 0  # 4/10 < 0.5
        assert tile.permanent_water[1, 0] == 1  # 8/10 > 0.5
        assert tile.permanent_water[1, 1] == 0  # 2/10 < 0.5


class TestGfmProcessorWriteOutputs:
    """Test GeoTIFF writing."""

    def test_write_outputs(self, tmp_path):
        tile = GfmProcessedTile(
            flood_fraction=np.random.rand(10, 10).astype(np.float32),
            quality_mask=np.ones((10, 10), dtype=np.uint8),
            permanent_water=np.zeros((10, 10), dtype=np.uint8),
            transform=from_bounds(10, 20, 11, 21, 10, 10),
            crs="EPSG:4326",
            shape=(10, 10),
            cloud_fraction=0.05,
        )

        proc = GfmRasterProcessor(bbox=(10, 20, 11, 21))
        paths = proc._write_outputs(tile, "test_event", "20240101", tmp_path)

        assert paths.flood_fraction is not None
        assert paths.flood_fraction.exists()
        assert paths.quality_mask is not None
        assert paths.quality_mask.exists()
        assert paths.permanent_water is not None
        assert paths.permanent_water.exists()

        # Verify file content
        import rasterio

        with rasterio.open(str(paths.flood_fraction)) as ds:
            assert ds.crs.to_epsg() == 4326
            assert ds.count == 1
            data = ds.read(1)
            assert data.shape == (10, 10)


class TestGfmProcessorAggregation:
    """Test tile aggregation logic."""

    def test_aggregate_mean_flood(self):
        """Aggregate flood_fraction should be the mean across tiles."""
        t = from_bounds(0, 0, 1, 1, 5, 5)
        tile1 = GfmProcessedTile(
            flood_fraction=np.full((5, 5), 0.2, dtype=np.float32),
            quality_mask=np.ones((5, 5), dtype=np.uint8),
            permanent_water=np.zeros((5, 5), dtype=np.uint8),
            transform=t,
            crs="EPSG:4326",
            shape=(5, 5),
        )
        tile2 = GfmProcessedTile(
            flood_fraction=np.full((5, 5), 0.8, dtype=np.float32),
            quality_mask=np.ones((5, 5), dtype=np.uint8),
            permanent_water=np.zeros((5, 5), dtype=np.uint8),
            transform=t,
            crs="EPSG:4326",
            shape=(5, 5),
        )

        result = GfmRasterProcessor.aggregate_tiles([tile1, tile2])
        np.testing.assert_allclose(result.flood_fraction, 0.5, atol=1e-6)

    def test_aggregate_quality_or(self):
        """Quality mask should be OR across tiles."""
        t = from_bounds(0, 0, 1, 1, 5, 5)
        qm1 = np.array([[1, 0], [0, 1]], dtype=np.uint8)
        qm2 = np.array([[0, 1], [1, 0]], dtype=np.uint8)

        tile1 = GfmProcessedTile(
            flood_fraction=np.zeros((2, 2), dtype=np.float32),
            quality_mask=qm1,
            permanent_water=np.zeros((2, 2), dtype=np.uint8),
            transform=from_bounds(0, 0, 1, 1, 2, 2),
            crs="EPSG:4326",
            shape=(2, 2),
        )
        tile2 = GfmProcessedTile(
            flood_fraction=np.zeros((2, 2), dtype=np.float32),
            quality_mask=qm2,
            permanent_water=np.zeros((2, 2), dtype=np.uint8),
            transform=from_bounds(0, 0, 1, 1, 2, 2),
            crs="EPSG:4326",
            shape=(2, 2),
        )

        result = GfmRasterProcessor.aggregate_tiles([tile1, tile2])
        np.testing.assert_array_equal(result.quality_mask, np.ones((2, 2), dtype=np.uint8))
