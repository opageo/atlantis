"""Tests for the MODIS raster processor."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from atlantis.fetchers.modis.processor import (
    INSUFFICIENT_DATA_CODE,
    MODIS_TILE_DEGREES,
    ModisRasterProcessor,
    ProcessedTile,
    _resolve_tile_path,
    modis_tiles_for_bbox,
    parse_hv_from_filename,
    tile_bounds_from_hv,
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
        nodata=INSUFFICIENT_DATA_CODE,
    ) as dst:
        dst.write(data, 1)


# ── Tile-grid helpers ────────────────────────────────────────────────────


class TestModisTilesForBbox:
    def test_pakistan_floods_2022_bbox(self):
        # bbox (66W, 22S, 72N, 31N) → h=24..25 / v=05..06
        tiles = modis_tiles_for_bbox((66.0, 22.0, 72.0, 31.0))
        assert (24, 5) in tiles
        assert (24, 6) in tiles
        assert (25, 5) in tiles
        assert (25, 6) in tiles

    def test_single_tile_bbox(self):
        # Small AOI inside h09v05.
        tiles = modis_tiles_for_bbox((-90.0, 5.0, -88.0, 7.0))
        assert tiles == [(9, 8)]

    def test_dateline_crossing_raises(self):
        with pytest.raises(ValueError, match="antimeridian"):
            modis_tiles_for_bbox((170.0, -10.0, -170.0, 10.0))

    def test_inverted_lat_raises(self):
        with pytest.raises(ValueError):
            modis_tiles_for_bbox((0.0, 30.0, 1.0, 20.0))


class TestTileBoundsFromHv:
    def test_origin_tile(self):
        bounds = tile_bounds_from_hv(0, 0)
        assert bounds == (-180.0, 80.0, -170.0, 90.0)

    def test_h24v05(self):
        west, south, east, north = tile_bounds_from_hv(24, 5)
        assert west == -180.0 + 24 * MODIS_TILE_DEGREES
        assert north == 90.0 - 5 * MODIS_TILE_DEGREES

    def test_invalid_hv_raises(self):
        with pytest.raises(ValueError):
            tile_bounds_from_hv(99, 0)
        with pytest.raises(ValueError):
            tile_bounds_from_hv(0, 99)


class TestParseHvFromFilename:
    def test_legacy_laads(self):
        assert parse_hv_from_filename("MCDWD_L3.A2024235.h24v05.061.hdf") == (24, 5)

    def test_lance_with_timestamp(self):
        assert parse_hv_from_filename("MCDWD_L3_F2_NRT.A2026032.h09v05.061.2026032142200.tif") == (9, 5)

    def test_no_match(self):
        assert parse_hv_from_filename("random_file.tif") is None


# ── _resolve_tile_path ───────────────────────────────────────────────────


class TestResolveTilePath:
    def test_local_path(self):
        p = Path("/data/tile.tif")
        assert _resolve_tile_path(p) == "/data/tile.tif"

    def test_remote_https(self):
        url = "https://example.com/tile.tif"
        assert _resolve_tile_path(url) == "/vsicurl/https://example.com/tile.tif"

    def test_already_vsicurl(self):
        url = "/vsicurl/https://example.com/tile.tif"
        assert _resolve_tile_path(url) == url

    def test_hdf4_subdataset_uri_passthrough(self):
        uri = 'HDF4_EOS:EOS_GRID:"foo.hdf":Grid_Water_Composite:Flood_2Day_250m'
        assert _resolve_tile_path(uri) == uri


# ── ModisRasterProcessor (GeoTIFF inputs) ────────────────────────────────


class TestModisRasterProcessor:
    def test_init_default(self):
        geom = box(-1.0, -1.0, 1.0, 1.0)
        processor = ModisRasterProcessor(area_geometry=geom)
        assert processor.classify is False
        assert processor.composite == "F2"

    def test_init_unknown_composite_raises(self):
        with pytest.raises(ValueError, match="composite"):
            ModisRasterProcessor(area_geometry=box(-1, -1, 1, 1), composite="FX")

    def test_process_no_tiles(self, tmp_path):
        processor = ModisRasterProcessor(area_geometry=box(-1, -1, 1, 1))
        assert (
            processor.process_tiles(tile_paths=[], event_id="test", date_token="20240822", output_dir=tmp_path) is None
        )

    def test_process_tiles_no_classify_raw(self, tmp_path):
        geom = box(-1.0, -1.0, 1.0, 1.0)
        processor = ModisRasterProcessor(area_geometry=geom, classify=False)

        tile_path = tmp_path / "tile.tif"
        # 4×4 raster of mixed codes.
        data = np.array(
            [
                [0, 1, 2, 3],
                [3, 3, 2, 1],
                [0, 0, 1, 1],
                [255, 255, 0, 3],
            ],
            dtype=np.uint8,
        )
        _write_tile(tile_path, -1.0, -1.0, 1.0, 1.0, data)

        result = processor.process_tiles(
            tile_paths=[tile_path], event_id="test", date_token="20240822", output_dir=tmp_path
        )
        assert result is not None
        assert result.paths.raw is not None and result.paths.raw.exists()
        assert result.paths.flood_fraction is None
        assert result.paths.recurring_flood is None
        assert isinstance(result.metadata, TileMetadata)

    def test_process_tiles_classify_emits_all_layers(self, tmp_path):
        geom = box(-1.0, -1.0, 1.0, 1.0)
        processor = ModisRasterProcessor(area_geometry=geom, classify=True)

        tile_path = tmp_path / "tile.tif"
        data = np.array(
            [
                [0, 1, 2, 3],
                [3, 3, 2, 1],
                [0, 0, 1, 1],
                [255, 255, 0, 3],
            ],
            dtype=np.uint8,
        )
        _write_tile(tile_path, -1.0, -1.0, 1.0, 1.0, data)

        result = processor.process_tiles(
            tile_paths=[tile_path], event_id="test", date_token="20240822", output_dir=tmp_path
        )
        assert result is not None
        for path_attr in ("flood_fraction", "quality_mask", "permanent_water", "recurring_flood"):
            path = getattr(result.paths, path_attr)
            assert path is not None and path.exists(), f"{path_attr} not written"

        # Verify pixel-level semantics on the in-memory ProcessedTile.
        proc = result.processed
        # Flood (class 3) count: 4 in our 4×4 (positions (0,3),(1,0),(1,1),(3,3))
        assert int((proc.flood_fraction > 0).sum()) == 4
        # Recurring (class 2): positions (0,2), (1,2)
        assert int(proc.recurring_flood.sum()) == 2
        # Permanent (class 1): positions (0,1), (1,3), (2,2), (2,3)
        assert int(proc.permanent_water.sum()) == 4
        # quality_mask: HAND-masked / cloud (255) drops to 0; everything else 1.
        # Two 255 pixels at (3,0) and (3,1) → 14 valid pixels.
        assert int(proc.quality_mask.sum()) == 14

    def test_hand_masked_pixels_drop_from_quality(self, tmp_path):
        geom = box(-1.0, -1.0, 1.0, 1.0)
        processor = ModisRasterProcessor(area_geometry=geom, classify=True)
        tile_path = tmp_path / "tile.tif"
        # All 255: no flood at all, quality_mask should be all zeros.
        data = np.full((4, 4), INSUFFICIENT_DATA_CODE, dtype=np.uint8)
        _write_tile(tile_path, -1.0, -1.0, 1.0, 1.0, data)
        result = processor.process_tiles(
            tile_paths=[tile_path], event_id="test", date_token="20240822", output_dir=tmp_path
        )
        assert result is not None
        assert int(result.processed.quality_mask.sum()) == 0
        assert int((result.processed.flood_fraction > 0).sum()) == 0

    def test_aggregate_tiles_mean_and_mode(self):
        # Two tiles with shared transform and three pixels each.
        transform = from_origin(0.0, 1.0, 1.0, 1.0)
        t1 = ProcessedTile(
            transform=transform,
            crs="EPSG:4326",
            cloud_fraction=0.2,
            flood_fraction=np.array([1.0, 0.0, 0.0], dtype=np.float32),
            quality_mask=np.array([1, 1, 0], dtype=np.uint8),
            permanent_water=np.array([0, 1, 0], dtype=np.uint8),
            recurring_flood=np.array([0, 0, 1], dtype=np.uint8),
        )
        t2 = ProcessedTile(
            transform=transform,
            crs="EPSG:4326",
            cloud_fraction=0.4,
            flood_fraction=np.array([1.0, 0.0, 1.0], dtype=np.float32),
            quality_mask=np.array([1, 0, 0], dtype=np.uint8),
            permanent_water=np.array([0, 1, 0], dtype=np.uint8),
            recurring_flood=np.array([0, 0, 1], dtype=np.uint8),
        )
        agg = ModisRasterProcessor.aggregate_tiles([t1, t2])
        assert pytest.approx(agg.flood_fraction.tolist()) == [1.0, 0.0, 0.5]
        # Mode along axis 0 — ties broken by lowest value via argmax.
        assert agg.quality_mask.tolist() == [1, 0, 0]
        assert agg.permanent_water.tolist() == [0, 1, 0]
        assert agg.recurring_flood.tolist() == [0, 0, 1]
        assert agg.cloud_fraction == pytest.approx(0.3, rel=1e-6)

    def test_aggregate_tiles_empty_raises(self):
        with pytest.raises(ValueError):
            ModisRasterProcessor.aggregate_tiles([])

    def test_aggregate_single_returns_input(self):
        transform = from_origin(0.0, 1.0, 1.0, 1.0)
        t1 = ProcessedTile(
            transform=transform,
            crs="EPSG:4326",
            cloud_fraction=0.0,
            flood_fraction=np.zeros((2, 2), dtype=np.float32),
        )
        assert ModisRasterProcessor.aggregate_tiles([t1]) is t1
