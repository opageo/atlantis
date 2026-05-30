"""Tests for the VIIRS raster processor."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from atlantis.fetchers.viirs.processor import (
    ProcessedTile,
    ViirsRasterProcessor,
    _resolve_tile_path,
)
from atlantis.models.metadata import TileMetadata


def _write_tile(path: Path, west: float, south: float, east: float, north: float, data: np.ndarray) -> None:
    height, width = data.shape
    transform = from_origin(west, north, (east - west) / width, (north - south) / height)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(data, 1)


class TestResolveTilePath:
    def test_local_path(self):
        p = Path("/data/tile.tif")
        assert _resolve_tile_path(p) == "/data/tile.tif"

    def test_remote_http(self):
        url = "https://example.com/tile.tif"
        assert _resolve_tile_path(url) == "/vsicurl/https://example.com/tile.tif"

    def test_remote_https(self):
        url = "https://example.com/tile.tif"
        assert _resolve_tile_path(url) == "/vsicurl/https://example.com/tile.tif"

    def test_already_vsicurl(self):
        url = "/vsicurl/https://example.com/tile.tif"
        assert _resolve_tile_path(url) == url

    def test_plain_string_path(self):
        assert _resolve_tile_path("/data/tile.tif") == "/data/tile.tif"


class TestViirsRasterProcessor:
    def test_init(self):
        geom = box(105.0, 28.0, 125.0, 38.0)
        processor = ViirsRasterProcessor(area_geometry=geom)
        assert processor.area_geometry == geom
        assert processor.crs == "EPSG:4326"
        assert processor.classify is False
        assert processor.flood_min_code == 160

    def test_init_classify(self):
        geom = box(105.0, 28.0, 125.0, 38.0)
        processor = ViirsRasterProcessor(area_geometry=geom, classify=True, flood_min_code=101)
        assert processor.classify is True
        assert processor.flood_min_code == 101

    def test_process_tiles_no_tiles(self, tmp_path):
        geom = box(105.0, 28.0, 125.0, 38.0)
        processor = ViirsRasterProcessor(area_geometry=geom)
        result = processor.process_tiles(tile_paths=[], event_id="test", date_token="20200722", output_dir=tmp_path)
        assert result is None

    def test_process_tiles_single_tile_raw(self, tmp_path):
        """Single tile, no classification – should write raw.tif."""
        geom = box(105.0, 28.0, 115.0, 38.0)
        processor = ViirsRasterProcessor(area_geometry=geom, classify=False)

        tile_path = tmp_path / "tile.tif"
        data = np.full((10, 10), 170, dtype=np.uint8)
        _write_tile(tile_path, 105.0, 28.0, 115.0, 38.0, data)

        result = processor.process_tiles(
            tile_paths=[tile_path], event_id="test", date_token="20200722", output_dir=tmp_path
        )
        assert result is not None
        paths, metadata = result
        assert paths.raw is not None
        assert paths.raw.exists()
        assert paths.flood_extent is None
        assert paths.quality_mask is None
        assert paths.permanent_water is None
        assert isinstance(metadata, TileMetadata)

    def test_process_tiles_classify(self, tmp_path):
        """Single tile with classification – should write all three masks."""
        geom = box(105.0, 28.0, 115.0, 38.0)
        processor = ViirsRasterProcessor(area_geometry=geom, classify=True, flood_min_code=101)

        tile_path = tmp_path / "tile.tif"
        data = np.full((10, 10), 170, dtype=np.uint8)
        _write_tile(tile_path, 105.0, 28.0, 115.0, 38.0, data)

        result = processor.process_tiles(
            tile_paths=[tile_path], event_id="test", date_token="20200722", output_dir=tmp_path
        )
        assert result is not None
        paths, metadata = result
        assert paths.raw is None
        assert paths.flood_extent is not None
        assert paths.flood_extent.exists()
        assert paths.quality_mask is not None
        assert paths.quality_mask.exists()
        assert paths.permanent_water is not None
        assert paths.permanent_water.exists()

    def test_mosaic_two_tiles(self, tmp_path):
        """Two adjacent tiles should be mosaicked, then clipped."""
        geom = box(105.0, 28.0, 125.0, 38.0)
        processor = ViirsRasterProcessor(area_geometry=geom, classify=True, flood_min_code=101)

        tile1 = tmp_path / "tile1.tif"
        _write_tile(tile1, 105.0, 28.0, 115.0, 38.0, np.full((10, 10), 170, dtype=np.uint8))
        tile2 = tmp_path / "tile2.tif"
        _write_tile(tile2, 115.0, 28.0, 125.0, 38.0, np.full((10, 10), 17, dtype=np.uint8))

        result = processor.process_tiles(
            tile_paths=[tile1, tile2], event_id="test", date_token="20200722", output_dir=tmp_path
        )
        assert result is not None
        paths, metadata = result
        assert paths.flood_extent is not None
        assert paths.flood_extent.exists()

    def test_classify_pixels_flood(self, tmp_path):
        """Verify classification logic for known pixel codes."""
        geom = box(105.0, 28.0, 115.0, 38.0)
        processor = ViirsRasterProcessor(area_geometry=geom, classify=True, flood_min_code=101)

        # Create tile with mixed codes
        tile_path = tmp_path / "mixed.tif"
        data = np.array(
            [
                [1, 17, 30, 99],  # fill, permanent water, cloud, open water
                [101, 160, 200, 20],  # flood (1%), flood (60%), flood (100%), seasonal water
            ],
            dtype=np.uint8,
        )
        _write_tile(tile_path, 105.0, 28.0, 115.0, 38.0, data)

        result = processor.process_tiles(
            tile_paths=[tile_path], event_id="test", date_token="20200722", output_dir=tmp_path
        )
        assert result is not None
        paths, metadata = result

        # Read back and verify
        with rasterio.open(paths.flood_extent) as src:
            flood = src.read(1)
        with rasterio.open(paths.quality_mask) as src:
            quality = src.read(1)
        with rasterio.open(paths.permanent_water) as src:
            water = src.read(1)

        # Flood extent: codes 101, 160, 200 → 1; others → 0
        expected_flood = np.array([[0, 0, 0, 0], [1, 1, 1, 0]], dtype=np.uint8)
        np.testing.assert_array_equal(flood, expected_flood)

        # Quality: fill(1) and cloud(30) → 0; others → 1
        expected_quality = np.array([[0, 1, 0, 1], [1, 1, 1, 1]], dtype=np.uint8)
        np.testing.assert_array_equal(quality, expected_quality)

        # Permanent water: code 17 → 1
        expected_water = np.array([[0, 1, 0, 0], [0, 0, 0, 0]], dtype=np.uint8)
        np.testing.assert_array_equal(water, expected_water)

    def test_classify_conservative_threshold(self, tmp_path):
        """With flood_min_code=160, only codes ≥160 are flood."""
        geom = box(105.0, 28.0, 115.0, 38.0)
        processor = ViirsRasterProcessor(area_geometry=geom, classify=True, flood_min_code=160)

        tile_path = tmp_path / "conservative.tif"
        data = np.array([[101, 130, 160, 200]], dtype=np.uint8)
        _write_tile(tile_path, 105.0, 28.0, 115.0, 38.0, data)

        result = processor.process_tiles(
            tile_paths=[tile_path], event_id="test", date_token="20200722", output_dir=tmp_path
        )
        assert result is not None
        paths, metadata = result

        with rasterio.open(paths.flood_extent) as src:
            flood = src.read(1)
        expected = np.array([[0, 0, 1, 1]], dtype=np.uint8)
        np.testing.assert_array_equal(flood, expected)

    def test_metadata_building(self, tmp_path):
        """Verify metadata fields populated correctly."""
        geom = box(105.0, 28.0, 115.0, 38.0)
        processor = ViirsRasterProcessor(area_geometry=geom, classify=True)

        tile_path = tmp_path / "meta.tif"
        _write_tile(tile_path, 105.0, 28.0, 115.0, 38.0, np.full((10, 10), 170, dtype=np.uint8))

        result = processor.process_tiles(
            tile_paths=[tile_path], event_id="my_event", date_token="20200722", output_dir=tmp_path
        )
        assert result is not None
        paths, metadata = result
        assert metadata.event_id == "my_event"
        assert metadata.source_id == "viirs"
        assert metadata.crs == "EPSG:4326"
        assert metadata.permanent_water_mask_available is True
        assert 0.0 <= metadata.cloud_fraction <= 1.0

    def test_output_tif_properties(self, tmp_path):
        """Verify GeoTIFF properties (CRS, compression, dtype, nodata)."""
        geom = box(105.0, 28.0, 115.0, 38.0)
        processor = ViirsRasterProcessor(area_geometry=geom, classify=True)

        tile_path = tmp_path / "props.tif"
        _write_tile(tile_path, 105.0, 28.0, 115.0, 38.0, np.full((10, 10), 170, dtype=np.uint8))

        result = processor.process_tiles(
            tile_paths=[tile_path], event_id="test", date_token="20200722", output_dir=tmp_path
        )
        assert result is not None
        paths, metadata = result

        with rasterio.open(paths.flood_extent) as src:
            assert src.crs.to_string() == "EPSG:4326"
            assert src.dtypes[0] == "uint8"
            assert src.compression.value.upper() == "LZW"
            assert src.nodata == 0


class TestProcessedTile:
    def test_defaults(self):
        tile = ProcessedTile(
            transform=rasterio.Affine(1, 0, 0, 0, -1, 10),
            crs="EPSG:4326",
            cloud_fraction=0.0,
        )
        assert tile.raw is None
        assert tile.flood_extent is None
        assert tile.quality_mask is None
        assert tile.permanent_water is None

    def test_frozen(self):
        tile = ProcessedTile(
            transform=rasterio.Affine(1, 0, 0, 0, -1, 10),
            crs="EPSG:4326",
            cloud_fraction=0.0,
        )
        with pytest.raises(AttributeError):
            tile.raw = np.zeros((5, 5))  # type: ignore[misc]
