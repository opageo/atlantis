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

    def test_init_classify(self):
        geom = box(105.0, 28.0, 125.0, 38.0)
        processor = ViirsRasterProcessor(area_geometry=geom, classify=True)
        assert processor.classify is True

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
        paths, metadata = result.paths, result.metadata
        assert paths.raw is not None
        assert paths.raw.exists()
        assert paths.flood_fraction is None
        assert paths.water_fraction is None
        assert paths.exclusion_mask is None
        assert paths.reference_water is None
        assert isinstance(metadata, TileMetadata)

    def test_process_tiles_classify(self, tmp_path):
        """Single tile with classification – should write the new core layers."""
        geom = box(105.0, 28.0, 115.0, 38.0)
        processor = ViirsRasterProcessor(area_geometry=geom, classify=True)

        tile_path = tmp_path / "tile.tif"
        data = np.full((10, 10), 170, dtype=np.uint8)
        _write_tile(tile_path, 105.0, 28.0, 115.0, 38.0, data)

        result = processor.process_tiles(
            tile_paths=[tile_path], event_id="test", date_token="20200722", output_dir=tmp_path
        )
        assert result is not None
        paths = result.paths
        assert paths.raw is None
        assert paths.water_fraction is not None
        assert paths.water_fraction.exists()
        assert paths.flood_fraction is not None
        assert paths.flood_fraction.exists()
        assert paths.exclusion_mask is not None
        assert paths.exclusion_mask.exists()
        assert paths.reference_water is not None
        assert paths.reference_water.exists()

    def test_mosaic_two_tiles(self, tmp_path):
        """Two adjacent tiles should be mosaicked, then clipped."""
        geom = box(105.0, 28.0, 125.0, 38.0)
        processor = ViirsRasterProcessor(area_geometry=geom, classify=True)

        tile1 = tmp_path / "tile1.tif"
        _write_tile(tile1, 105.0, 28.0, 115.0, 38.0, np.full((10, 10), 170, dtype=np.uint8))
        tile2 = tmp_path / "tile2.tif"
        _write_tile(tile2, 115.0, 28.0, 125.0, 38.0, np.full((10, 10), 17, dtype=np.uint8))

        result = processor.process_tiles(
            tile_paths=[tile1, tile2], event_id="test", date_token="20200722", output_dir=tmp_path
        )
        assert result is not None
        paths = result.paths
        assert paths.flood_fraction is not None
        assert paths.flood_fraction.exists()

    def test_classify_pixels_flood(self, tmp_path):
        """Verify continuous flood fraction for known pixel codes."""
        geom = box(105.0, 28.0, 115.0, 38.0)
        processor = ViirsRasterProcessor(area_geometry=geom, classify=True)

        # Create tile with mixed codes (per the embedded NOAA TIFF legend:
        # 17=Vegetation, 20=Snow/ice, 30=Cloud, 99=NormalWater/permanent water).
        tile_path = tmp_path / "mixed.tif"
        data = np.array(
            [
                [1, 17, 30, 99],  # fill, vegetation, cloud, permanent water
                [101, 160, 200, 20],  # flood (1%), flood (60%), flood (100%), snow/ice
            ],
            dtype=np.uint8,
        )
        _write_tile(tile_path, 105.0, 28.0, 115.0, 38.0, data)

        result = processor.process_tiles(
            tile_paths=[tile_path], event_id="test", date_token="20200722", output_dir=tmp_path
        )
        assert result is not None
        paths = result.paths

        # Read back and verify
        with rasterio.open(paths.water_fraction) as src:
            water = src.read(1)
        with rasterio.open(paths.flood_fraction) as src:
            flood = src.read(1)
        with rasterio.open(paths.exclusion_mask) as src:
            exclusion = src.read(1)
        with rasterio.open(paths.reference_water) as src:
            reference = src.read(1)

        # Code 17 (vegetation) and code 20 (snow/ice) are both treated as
        # invalid by _invalid_mask → NaN/255.
        expected_water = np.array([[255, 255, 255, 100], [1, 60, 100, 255]], dtype=np.uint8)
        np.testing.assert_array_equal(water, expected_water)

        # Flood fraction stored as uint8 percentage: codes 101->1, 160->60,
        # 200->100; fill/cloud/vegetation/snow-ice remain nodata=255.
        expected_flood = np.array([[255, 255, 255, 0], [1, 60, 100, 255]], dtype=np.uint8)
        np.testing.assert_array_equal(flood, expected_flood)

        # Exclusion: fill(1), vegetation(17), cloud(30), and snow/ice(20) → 1; others → 0
        expected_exclusion = np.array([[1, 1, 1, 0], [0, 0, 0, 1]], dtype=np.uint8)
        np.testing.assert_array_equal(exclusion, expected_exclusion)

        # Reference water: code 99 → 1 (per embedded NOAA TIFF legend)
        expected_reference = np.array([[0, 0, 0, 1], [0, 0, 0, 0]], dtype=np.uint8)
        np.testing.assert_array_equal(reference, expected_reference)

    def test_classify_continuous_fractions(self, tmp_path):
        """All flood codes 101-200 produce continuous fractions; threshold no longer gates output."""
        geom = box(105.0, 28.0, 115.0, 38.0)
        processor = ViirsRasterProcessor(area_geometry=geom, classify=True)

        tile_path = tmp_path / "fractions.tif"
        data = np.array([[101, 130, 160, 200]], dtype=np.uint8)
        _write_tile(tile_path, 105.0, 28.0, 115.0, 38.0, data)

        result = processor.process_tiles(
            tile_paths=[tile_path], event_id="test", date_token="20200722", output_dir=tmp_path
        )
        assert result is not None
        paths = result.paths

        with rasterio.open(paths.flood_fraction) as src:
            flood = src.read(1)
        # On-disk: uint8 percentage (0–100); codes 101->1, 130->30, 160->60, 200->100
        expected = np.array([[1, 30, 60, 100]], dtype=np.uint8)
        np.testing.assert_array_equal(flood, expected)

    def test_aggregate_tiles_skips_missing_observations(self):
        """Cloud/fill dates should not dilute aggregated flood_fraction or masks."""
        transform = rasterio.Affine(1, 0, 0, 0, -1, 1)
        clear = ProcessedTile(
            water_fraction=np.array([[0.6]], dtype=np.float32),
            flood_fraction=np.array([[0.6]], dtype=np.float32),
            exclusion_mask=np.array([[0]], dtype=np.uint8),
            reference_water=np.array([[0]], dtype=np.uint8),
            transform=transform,
            crs="EPSG:4326",
            cloud_fraction=0.0,
        )
        cloudy = ProcessedTile(
            water_fraction=np.array([[np.nan]], dtype=np.float32),
            flood_fraction=np.array([[np.nan]], dtype=np.float32),
            exclusion_mask=np.array([[1]], dtype=np.uint8),
            reference_water=np.array([[0]], dtype=np.uint8),
            transform=transform,
            crs="EPSG:4326",
            cloud_fraction=1.0,
        )

        result = ViirsRasterProcessor.aggregate_tiles([clear, cloudy])

        assert result.water_fraction is not None
        assert result.flood_fraction is not None
        assert result.exclusion_mask is not None
        assert result.reference_water is not None
        assert result.water_fraction[0, 0] == pytest.approx(0.6, rel=1e-6)
        assert result.flood_fraction[0, 0] == pytest.approx(0.6, rel=1e-6)
        assert int(result.exclusion_mask[0, 0]) == 0
        assert int(result.reference_water[0, 0]) == 0

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
        metadata = result.metadata
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
        paths = result.paths

        with rasterio.open(paths.flood_fraction) as src:
            assert src.crs.to_string() == "EPSG:4326"
            assert src.dtypes[0] == "uint8"
            assert src.compression.value.upper() == "LZW"
            assert src.nodata == 255


class TestProcessedTile:
    def test_defaults(self):
        tile = ProcessedTile(
            transform=rasterio.Affine(1, 0, 0, 0, -1, 10),
            crs="EPSG:4326",
            cloud_fraction=0.0,
        )
        assert tile.raw is None
        assert tile.water_fraction is None
        assert tile.flood_fraction is None
        assert tile.exclusion_mask is None
        assert tile.reference_water is None

    def test_frozen(self):
        tile = ProcessedTile(
            transform=rasterio.Affine(1, 0, 0, 0, -1, 10),
            crs="EPSG:4326",
            cloud_fraction=0.0,
        )
        with pytest.raises(AttributeError):
            tile.raw = np.zeros((5, 5))  # type: ignore[misc]
