"""Tests for the GFM raster processor."""

import numpy as np
import xarray as xr
from rasterio.transform import from_bounds

from atlantis.fetchers.gfm.processor import (
    GFM_DRY,
    GFM_FLOOD,
    GFM_NODATA,
    GFM_PERMANENT_WATER,
    GFM_WATER,
    GfmProcessedTile,
    GfmRasterProcessor,
    _masked_max,
)


class TestGfmProcessorClassify:
    """Test the classification logic directly."""

    def test_all_flood(self):
        """All valid pixels are flood → flood_fraction = 1.0."""
        proc = GfmRasterProcessor(bbox=(0, 0, 1, 1))
        flood_count = np.full((5, 5), 10, dtype=np.uint32)
        water_count = np.full((5, 5), 10, dtype=np.uint32)
        valid_count = np.full((5, 5), 10, dtype=np.uint32)

        import xarray as xr

        coords = {"x": np.linspace(0, 1, 5), "y": np.linspace(1, 0, 5)}
        dims = ("y", "x")
        mock_coords = xr.DataArray(np.zeros((5, 5)), coords=coords, dims=dims).coords

        tile = proc._classify(
            flood_count,
            water_count,
            valid_count,
            mock_coords,
            dims,
            reference_water_codes=np.zeros((5, 5), dtype=np.uint8),
        )

        assert tile.water_fraction.shape == (5, 5)
        np.testing.assert_allclose(tile.water_fraction, 1.0)
        assert tile.flood_fraction.shape == (5, 5)
        np.testing.assert_allclose(tile.flood_fraction, 1.0)
        np.testing.assert_array_equal(tile.reference_water, 0)

    def test_all_dry(self):
        """All valid pixels are dry → flood_fraction = 0.0."""
        proc = GfmRasterProcessor(bbox=(0, 0, 1, 1))
        flood_count = np.zeros((5, 5), dtype=np.uint32)
        water_count = np.zeros((5, 5), dtype=np.uint32)
        valid_count = np.full((5, 5), 10, dtype=np.uint32)

        import xarray as xr

        coords = {"x": np.linspace(0, 1, 5), "y": np.linspace(1, 0, 5)}
        dims = ("y", "x")
        mock_coords = xr.DataArray(np.zeros((5, 5)), coords=coords, dims=dims).coords

        tile = proc._classify(
            flood_count,
            water_count,
            valid_count,
            mock_coords,
            dims,
            reference_water_codes=np.zeros((5, 5), dtype=np.uint8),
        )

        np.testing.assert_allclose(tile.water_fraction, 0.0)
        np.testing.assert_allclose(tile.flood_fraction, 0.0)

    def test_no_valid_data(self):
        """No valid observations → fractions = NaN and cloud_fraction = 1."""
        proc = GfmRasterProcessor(bbox=(0, 0, 1, 1))
        flood_count = np.zeros((5, 5), dtype=np.uint32)
        water_count = np.zeros((5, 5), dtype=np.uint32)
        valid_count = np.zeros((5, 5), dtype=np.uint32)

        import xarray as xr

        coords = {"x": np.linspace(0, 1, 5), "y": np.linspace(1, 0, 5)}
        dims = ("y", "x")
        mock_coords = xr.DataArray(np.zeros((5, 5)), coords=coords, dims=dims).coords

        tile = proc._classify(
            flood_count,
            water_count,
            valid_count,
            mock_coords,
            dims,
            reference_water_codes=np.full((5, 5), GFM_NODATA, dtype=np.uint8),
        )

        assert np.all(np.isnan(tile.water_fraction))
        assert np.all(np.isnan(tile.flood_fraction))
        assert tile.cloud_fraction == 1.0

    def test_mixed_data(self):
        """Mix of flood and dry → fractional values."""
        proc = GfmRasterProcessor(bbox=(0, 0, 1, 1))
        flood_count = np.array([[3, 0], [1, 5]], dtype=np.uint32)
        water_count = np.array([[5, 1], [4, 8]], dtype=np.uint32)
        valid_count = np.array([[10, 10], [10, 10]], dtype=np.uint32)

        import xarray as xr

        coords = {"x": np.linspace(0, 1, 2), "y": np.linspace(1, 0, 2)}
        dims = ("y", "x")
        mock_coords = xr.DataArray(np.zeros((2, 2)), coords=coords, dims=dims).coords

        tile = proc._classify(
            flood_count,
            water_count,
            valid_count,
            mock_coords,
            dims,
            reference_water_codes=np.zeros((2, 2), dtype=np.uint8),
        )

        np.testing.assert_allclose(tile.water_fraction[0, 0], 0.5)
        np.testing.assert_allclose(tile.water_fraction[0, 1], 0.1)
        np.testing.assert_allclose(tile.water_fraction[1, 0], 0.4)
        np.testing.assert_allclose(tile.water_fraction[1, 1], 0.8)
        np.testing.assert_allclose(tile.flood_fraction[0, 0], 0.3)
        np.testing.assert_allclose(tile.flood_fraction[0, 1], 0.0)
        np.testing.assert_allclose(tile.flood_fraction[1, 0], 0.1)
        np.testing.assert_allclose(tile.flood_fraction[1, 1], 0.5)

    def test_reference_water_preserves_native_codes(self):
        """Reference water is carried through under the shared layer name."""
        proc = GfmRasterProcessor(bbox=(0, 0, 1, 1))
        flood_count = np.zeros((2, 2), dtype=np.uint32)
        water_count = np.zeros((2, 2), dtype=np.uint32)
        valid_count = np.full((2, 2), 10, dtype=np.uint32)

        import xarray as xr

        coords = {"x": np.linspace(0, 1, 2), "y": np.linspace(1, 0, 2)}
        dims = ("y", "x")
        mock_coords = xr.DataArray(np.zeros((2, 2)), coords=coords, dims=dims).coords

        reference_codes = np.array([[2, 1], [0, GFM_NODATA]], dtype=np.uint8)
        tile = proc._classify(
            flood_count,
            water_count,
            valid_count,
            mock_coords,
            dims,
            reference_water_codes=reference_codes,
        )

        np.testing.assert_array_equal(tile.reference_water, reference_codes)


class TestGfmProcessorNativeMasks:
    """Lock down discrete GFM code handling before reprojection."""

    def test_build_native_masks_uses_discrete_codes(self):
        flood_native = xr.DataArray(
            np.array(
                [
                    [1, 0, GFM_NODATA],
                    [1, GFM_NODATA, 0],
                ],
                dtype=np.uint8,
            ),
            dims=("y", "x"),
        )
        water_native = xr.DataArray(
            np.array(
                [
                    [1, 1, GFM_NODATA],
                    [1, GFM_NODATA, 0],
                ],
                dtype=np.uint8,
            ),
            dims=("y", "x"),
        )
        reference_native = xr.DataArray(
            np.array(
                [
                    [0, GFM_PERMANENT_WATER, GFM_NODATA],
                    [GFM_PERMANENT_WATER, 1, 0],
                ],
                dtype=np.uint8,
            ),
            dims=("y", "x"),
        )

        flood_mask, water_mask, valid_mask = GfmRasterProcessor._build_native_masks(
            flood_native,
            water_native,
            reference_native,
        )

        np.testing.assert_array_equal(flood_mask.values, np.array([[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float32))
        np.testing.assert_array_equal(water_mask.values, np.array([[1.0, 1.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float32))
        np.testing.assert_array_equal(valid_mask.values, np.array([[1.0, 1.0, 0.0], [1.0, 1.0, 1.0]], dtype=np.float32))

    def test_mean_pool_preserves_flood_fraction_not_max_code(self):
        """Coarsening must mean-pool 0/1 masks, not max-pool nominal codes.

        A 2×2 block mixing flood (1) with nodata (255) must coarsen to the
        *fraction* of flood sub-pixels — not be wiped out because nodata is the
        numerically largest code (the old ``.max()`` behaviour).
        """
        flood_native = xr.DataArray(
            np.array([[GFM_FLOOD, GFM_NODATA], [GFM_DRY, GFM_FLOOD]], dtype=np.uint8),
            dims=("y", "x"),
        )
        water_native = xr.DataArray(
            np.array([[GFM_WATER, GFM_NODATA], [GFM_DRY, GFM_WATER]], dtype=np.uint8),
            dims=("y", "x"),
        )
        reference_native = xr.DataArray(
            np.array([[GFM_DRY, GFM_DRY], [GFM_PERMANENT_WATER, GFM_DRY]], dtype=np.uint8),
            dims=("y", "x"),
        )

        flood_mask, water_mask, valid_mask = GfmRasterProcessor._build_native_masks(
            flood_native,
            water_native,
            reference_native,
            coarsen_factor=2,
        )

        # 2 of 4 sub-pixels are flood, 2 of 4 are water, all 4 valid.
        np.testing.assert_allclose(flood_mask.values, np.array([[0.5]], dtype=np.float32))
        np.testing.assert_allclose(water_mask.values, np.array([[0.5]], dtype=np.float32))
        np.testing.assert_allclose(valid_mask.values, np.array([[1.0]], dtype=np.float32))

    def test_canonical_grid_snaps_bbox_outward(self):
        proc = GfmRasterProcessor(bbox=(10.003, 20.002, 10.031, 20.029))

        west, south, east, north = proc._snapped_bounds

        assert west <= 10.003
        assert south <= 20.002
        assert east >= 10.031
        assert north >= 20.029
        # Snapped bounds align to the processor's own canonical grid (~80 m),
        # anchored at lon0 = -180, lat0 = +90 with spacing target_resolution.
        res = proc.reprojector.target_resolution
        lon0 = proc.reprojector.global_grid_origin_lon
        lat0 = proc.reprojector.global_grid_origin_lat
        np.testing.assert_allclose((west - lon0) / res, round((west - lon0) / res), atol=1e-6)
        np.testing.assert_allclose((east - lon0) / res, round((east - lon0) / res), atol=1e-6)
        np.testing.assert_allclose((lat0 - north) / res, round((lat0 - north) / res), atol=1e-6)
        np.testing.assert_allclose((lat0 - south) / res, round((lat0 - south) / res), atol=1e-6)


class TestGfmProcessorWriteOutputs:
    """Test GeoTIFF writing."""

    def test_write_outputs(self, tmp_path):
        rng = np.random.default_rng(0)
        water_fraction = np.clip(rng.random((10, 10)).astype(np.float32) + 0.2, 0.0, 1.0)
        flood_fraction = rng.random((10, 10)).astype(np.float32)
        tile = GfmProcessedTile(
            water_fraction=water_fraction,
            flood_fraction=flood_fraction,
            reference_water=np.zeros((10, 10), dtype=np.uint8),
            extra_layers={"exclusion_mask": np.ones((10, 10), dtype=np.uint8)},
            transform=from_bounds(10, 20, 11, 21, 10, 10),
            crs="EPSG:4326",
            shape=(10, 10),
            cloud_fraction=0.05,
        )

        proc = GfmRasterProcessor(bbox=(10, 20, 11, 21))
        paths = proc._write_outputs(tile, "test_event", "20240101", tmp_path)

        assert paths.water_fraction is not None
        assert paths.water_fraction.exists()
        assert paths.flood_fraction is not None
        assert paths.flood_fraction.exists()
        assert paths.reference_water is not None
        assert paths.reference_water.exists()
        assert paths.extra["exclusion_mask"].exists()

        # Verify file content
        import rasterio

        with rasterio.open(str(paths.water_fraction)) as ds:
            assert ds.nodata == 255
            expected = np.rint(np.clip(water_fraction, 0.0, 1.0) * 100).astype(np.uint8)
            np.testing.assert_array_equal(ds.read(1), expected)

        with rasterio.open(str(paths.flood_fraction)) as ds:
            assert ds.crs.to_epsg() == 4326
            assert ds.count == 1
            data = ds.read(1)
            assert data.shape == (10, 10)
            # flood_fraction is encoded as uint8 percent (0-100), nodata 255.
            assert ds.dtypes[0] == "uint8"
            assert ds.nodata == 255
            expected = np.rint(np.clip(flood_fraction, 0.0, 1.0) * 100).astype(np.uint8)
            np.testing.assert_array_equal(data, expected)
            assert data.max() <= 100


class TestGfmProcessorAggregation:
    """Test tile aggregation logic."""

    def test_aggregate_mean_flood(self):
        """Aggregate flood_fraction should be the mean across tiles."""
        t = from_bounds(0, 0, 1, 1, 5, 5)
        tile1 = GfmProcessedTile(
            water_fraction=np.full((5, 5), 0.4, dtype=np.float32),
            flood_fraction=np.full((5, 5), 0.2, dtype=np.float32),
            reference_water=np.zeros((5, 5), dtype=np.uint8),
            transform=t,
            crs="EPSG:4326",
            shape=(5, 5),
        )
        tile2 = GfmProcessedTile(
            water_fraction=np.full((5, 5), 1.0, dtype=np.float32),
            flood_fraction=np.full((5, 5), 0.8, dtype=np.float32),
            reference_water=np.zeros((5, 5), dtype=np.uint8),
            transform=t,
            crs="EPSG:4326",
            shape=(5, 5),
        )

        result = GfmRasterProcessor.aggregate_tiles([tile1, tile2])
        np.testing.assert_allclose(result.water_fraction, 0.7, atol=1e-6)
        np.testing.assert_allclose(result.flood_fraction, 0.5, atol=1e-6)

    def test_aggregate_reference_water_and_advisory_flags(self):
        """Reference-water codes use masked max and advisory flags use bitwise OR."""
        _ = from_bounds(0, 0, 1, 1, 5, 5)
        rw1 = np.array([[0, 1], [GFM_NODATA, 0]], dtype=np.uint8)
        rw2 = np.array([[2, 0], [1, GFM_NODATA]], dtype=np.uint8)

        tile1 = GfmProcessedTile(
            water_fraction=np.zeros((2, 2), dtype=np.float32),
            flood_fraction=np.zeros((2, 2), dtype=np.float32),
            reference_water=rw1,
            extra_layers={"advisory_flags": np.array([[1, 2], [4, 8]], dtype=np.uint8)},
            transform=from_bounds(0, 0, 1, 1, 2, 2),
            crs="EPSG:4326",
            shape=(2, 2),
        )
        tile2 = GfmProcessedTile(
            water_fraction=np.zeros((2, 2), dtype=np.float32),
            flood_fraction=np.zeros((2, 2), dtype=np.float32),
            reference_water=rw2,
            extra_layers={"advisory_flags": np.array([[2, 1], [1, GFM_NODATA]], dtype=np.uint8)},
            transform=from_bounds(0, 0, 1, 1, 2, 2),
            crs="EPSG:4326",
            shape=(2, 2),
        )

        result = GfmRasterProcessor.aggregate_tiles([tile1, tile2])
        np.testing.assert_array_equal(result.reference_water, np.array([[2, 1], [1, 0]], dtype=np.uint8))
        np.testing.assert_array_equal(
            result.extra_layers["advisory_flags"],
            np.array([[3, 3], [5, 8]], dtype=np.uint8),
        )


# ── Native / raw-mode tests ───────────────────────────────────────────────────


class TestMaskedMax:
    """Tests for the _masked_max utility."""

    def test_both_valid_returns_max(self):
        a = np.array([0, 1, 0], dtype=np.uint8)
        b = np.array([1, 0, 0], dtype=np.uint8)
        result = _masked_max(a, b, nodata=255)
        np.testing.assert_array_equal(result, [1, 1, 0])

    def test_one_nodata_uses_other(self):
        a = np.array([255, 1, 255], dtype=np.uint8)
        b = np.array([0, 255, 1], dtype=np.uint8)
        result = _masked_max(a, b, nodata=255)
        np.testing.assert_array_equal(result, [0, 1, 1])

    def test_both_nodata_stays_nodata(self):
        a = np.array([255, 255], dtype=np.uint8)
        b = np.array([255, 255], dtype=np.uint8)
        result = _masked_max(a, b, nodata=255)
        np.testing.assert_array_equal(result, [255, 255])

    def test_output_dtype_is_uint8(self):
        a = np.array([0, 1], dtype=np.uint8)
        b = np.array([1, 0], dtype=np.uint8)
        assert _masked_max(a, b, nodata=255).dtype == np.uint8


class TestBuildNativeTile:
    """Tests for GfmRasterProcessor._build_native_tile."""

    def _make_proc(self):
        return GfmRasterProcessor(bbox=(0.0, 0.0, 1.0, 1.0))

    def test_fields_populated(self):
        proc = self._make_proc()
        efe = np.array([[GFM_FLOOD, 0], [0, GFM_NODATA]], dtype=np.uint8)
        rwm = np.array([[0, 0], [2, GFM_NODATA]], dtype=np.uint8)
        tile = proc._build_native_tile(efe, rwm)
        np.testing.assert_array_equal(tile.ensemble_flood_extent, efe)
        np.testing.assert_array_equal(tile.reference_water_mask, rwm)
        assert tile.water_fraction is None
        assert tile.flood_fraction is None
        assert tile.reference_water is None

    def test_cloud_fraction_proportional_to_nodata(self):
        proc = self._make_proc()
        efe = np.array([[GFM_NODATA, GFM_FLOOD], [GFM_FLOOD, GFM_FLOOD]], dtype=np.uint8)
        rwm = np.zeros_like(efe)
        tile = proc._build_native_tile(efe, rwm)
        # 1 out of 4 pixels is nodata → cloud_fraction = 0.25
        assert abs(tile.cloud_fraction - 0.25) < 1e-6


class TestAggregateNativeTiles:
    """Tests for aggregate_tiles in native mode."""

    def _make_native_tile(self, efe_vals, rwm_vals):
        t = from_bounds(0, 0, 1, 1, 2, 2)
        return GfmProcessedTile(
            ensemble_flood_extent=np.array(efe_vals, dtype=np.uint8).reshape(2, 2),
            reference_water_mask=np.array(rwm_vals, dtype=np.uint8).reshape(2, 2),
            transform=t,
            crs="EPSG:4326",
            shape=(2, 2),
            cloud_fraction=0.0,
        )

    def test_single_tile_returned_unchanged(self):
        tile = self._make_native_tile([0, 1, 0, 255], [0, 0, 2, 255])
        result = GfmRasterProcessor.aggregate_tiles([tile])
        assert result is tile

    def test_max_pool_across_dates(self):
        tile1 = self._make_native_tile([0, 255, 0, 1], [0, 255, 0, 2])
        tile2 = self._make_native_tile([1, 0, 255, 0], [2, 0, 255, 0])
        result = GfmRasterProcessor.aggregate_tiles([tile1, tile2])
        expected_efe = np.array([[1, 0], [0, 1]], dtype=np.uint8)
        expected_rwm = np.array([[2, 0], [0, 2]], dtype=np.uint8)
        np.testing.assert_array_equal(result.ensemble_flood_extent, expected_efe)
        np.testing.assert_array_equal(result.reference_water_mask, expected_rwm)

    def test_both_nodata_stays_nodata(self):
        tile1 = self._make_native_tile([255, 255, 255, 255], [255, 255, 255, 255])
        tile2 = self._make_native_tile([255, 255, 255, 255], [255, 255, 255, 255])
        result = GfmRasterProcessor.aggregate_tiles([tile1, tile2])
        np.testing.assert_array_equal(result.ensemble_flood_extent, 255)


class TestWriteOutputsNative:
    """Tests for _write_outputs in native mode."""

    def test_writes_native_band_files(self, tmp_path):
        t = from_bounds(10, 20, 11, 21, 4, 4)
        tile = GfmProcessedTile(
            ensemble_flood_extent=np.array(
                [[0, 1, 0, 255], [1, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 255]], dtype=np.uint8
            ),
            reference_water_mask=np.array([[0, 0, 2, 0], [0, 1, 0, 0], [2, 0, 0, 0], [0, 0, 0, 255]], dtype=np.uint8),
            transform=t,
            crs="EPSG:4326",
            shape=(4, 4),
            cloud_fraction=0.1,
        )
        proc = GfmRasterProcessor(bbox=(10, 20, 11, 21))
        paths = proc._write_outputs(tile, "evt", "20240101", tmp_path)

        assert paths.ensemble_flood_extent is not None and paths.ensemble_flood_extent.exists()
        assert paths.reference_water_mask is not None and paths.reference_water_mask.exists()
        assert paths.water_fraction is None
        assert paths.flood_fraction is None
        assert paths.reference_water is None

        import rasterio

        with rasterio.open(str(paths.ensemble_flood_extent)) as ds:
            assert ds.nodata == GFM_NODATA
            assert ds.dtypes[0] == "uint8"


class TestGfmRetryRead:
    """Test the transient-failure retry helpers."""

    def test_is_retryable_read_error_detects_http(self):
        """HTTP response code messages and rasterio IO errors are retryable."""
        from rasterio.errors import RasterioIOError

        proc = GfmRasterProcessor(bbox=(0, 0, 1, 1))
        assert proc._is_retryable_read_error(RasterioIOError("HTTP response code: 404"))
        assert proc._is_retryable_read_error(RasterioIOError("HTTP response code: 500"))
        assert proc._is_retryable_read_error(RasterioIOError("Connection reset"))
        assert not proc._is_retryable_read_error(ValueError("bad input"))

    def test_retry_read_succeeds_first_attempt(self):
        proc = GfmRasterProcessor(bbox=(0, 0, 1, 1))
        result = proc._retry_read(lambda: "ok", item_id="item-1", context="test")
        assert result == "ok"

    def test_retry_read_retries_then_succeeds(self, monkeypatch):
        proc = GfmRasterProcessor(bbox=(0, 0, 1, 1), max_retries=2)
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 2:
                from rasterio.errors import RasterioIOError

                raise RasterioIOError("HTTP response code: 503")
            return "recovered"

        monkeypatch.setattr("time.sleep", lambda _s: None)
        result = proc._retry_read(flaky, item_id="item-1", context="test")
        assert result == "recovered"
        assert len(calls) == 2

    def test_retry_read_returns_none_after_exhaustion(self, monkeypatch):
        proc = GfmRasterProcessor(bbox=(0, 0, 1, 1), max_retries=1)
        monkeypatch.setattr("time.sleep", lambda _s: None)

        def always_fails():
            from rasterio.errors import RasterioIOError

            raise RasterioIOError("HTTP response code: 404")

        result = proc._retry_read(always_fails, item_id="item-1", context="test")
        assert result is None

    def test_load_item_retries_then_skips(self, monkeypatch):
        """A persistently failing odc.stac.load returns None so the item is skipped."""
        import odc.stac
        from rasterio.errors import RasterioIOError

        proc = GfmRasterProcessor(bbox=(0, 0, 1, 1), max_retries=1)
        monkeypatch.setattr("time.sleep", lambda _s: None)

        class FakeItem:
            id = "fake-item"

        fake_aoi = type("Aoi", (), {"bounds": (0, 0, 1, 1)})()
        fake_crs = type("Crs", (), {"__name__": "EPSG:4326"})()

        calls = []

        def fake_load(*_args, **_kwargs):
            calls.append(1)
            raise RasterioIOError("HTTP response code: 404")

        monkeypatch.setattr(odc.stac, "load", fake_load)
        result = proc._load_item(FakeItem(), fake_aoi, fake_crs, 20.0)
        assert result is None
        assert len(calls) == 2  # initial + 1 retry
