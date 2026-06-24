"""Tests for the Harmoniser orchestration class."""

from __future__ import annotations

import numpy as np
import pytest
from rasterio.transform import from_bounds

from atlantis.config import HarmoniseConfig
from atlantis.harmoniser import Harmoniser
from atlantis.harmoniser.normaliser import Normaliser, NormaliserConfig
from atlantis.harmoniser.reprojector import Reprojector
from atlantis.harmoniser.tiler import Tiler

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_viirs_dataset(
    width: int = 100,
    height: int = 100,
    res: float = 0.004,
    west: float = 20.0,
    north: float = 35.4,
):
    """Create a realistic VIIRS-like dataset."""
    import rioxarray  # noqa: F401
    import xarray as xr

    east = west + width * res
    south = north - height * res

    np.random.seed(42)
    flood = np.random.choice([0, 1], size=(height, width), p=[0.9, 0.1]).astype(np.uint8)
    quality = np.ones((height, width), dtype=np.uint8)
    quality[:, :10] = 0

    transform = from_bounds(west, south, east, north, width, height)

    ds = xr.Dataset(
        {
            "flood_fraction": xr.DataArray(flood, dims=["y", "x"]),
            "quality_mask": xr.DataArray(quality, dims=["y", "x"]),
            "permanent_water": xr.DataArray(np.zeros((height, width), dtype=np.uint8), dims=["y", "x"]),
        },
        coords={
            "x": west + (np.arange(width) + 0.5) * res,
            "y": north - (np.arange(height) + 0.5) * res,
        },
        attrs={"source_id": "viirs", "cloud_fraction": 0.1},
    )
    ds.rio.write_crs("EPSG:4326", inplace=True)
    ds.rio.write_transform(transform, inplace=True)
    return ds


# ── Harmoniser tests ─────────────────────────────────────────────────────────


class TestHarmoniser:
    def test_init_defaults(self):
        h = Harmoniser()
        assert isinstance(h.config, HarmoniseConfig)
        assert h.reprojector.target_crs == "EPSG:4326"
        assert h.normaliser.config.normalise_range == (0.0, 1.0)

    def test_init_with_config(self):
        cfg = HarmoniseConfig()
        cfg.target_resolution = 0.008333333333333333  # 0.5 arcmin
        h = Harmoniser(config=cfg)
        assert h.reprojector.target_resolution == pytest.approx(0.008333333333333333)

    def test_init_with_prebuilt_components(self):
        r = Reprojector(target_crs="EPSG:4326", target_resolution=0.05)
        n = Normaliser(config=NormaliserConfig(normalise_range=(0.0, 2.0)))
        h = Harmoniser(reprojector=r, normaliser=n)
        assert h.reprojector.target_resolution == 0.05
        assert h.normaliser.config.normalise_range == (0.0, 2.0)

    def test_harmonise_full_pipeline(self):
        """End-to-end: resample + normalise + masks."""
        ds = _make_viirs_dataset()
        h = Harmoniser()
        ds_out = h.harmonise(ds, source_id="viirs")

        # Check output structure
        assert "flood_fraction" in ds_out.data_vars
        assert "quality_mask" in ds_out.data_vars
        assert "permanent_water" in ds_out.data_vars

        # Resolution reduced
        assert ds_out["flood_fraction"].shape[0] < ds["flood_fraction"].shape[0]
        assert ds_out["flood_fraction"].shape[1] < ds["flood_fraction"].shape[1]

        # Float32 flood extent in 0-1
        assert ds_out["flood_fraction"].dtype == np.float32
        assert ds_out["flood_fraction"].min().values >= 0.0
        assert ds_out["flood_fraction"].max().values <= 1.0

        # Provenance attrs
        assert ds_out.attrs["source_id"] == "viirs"
        assert ds_out.attrs["target_resolution_arcmin"] == 1.0
        assert ds_out.attrs["pipeline"] == "harmonise"

    def test_harmonise_file_roundtrip(self, tmp_path):
        """harmonise_file should read GeoTIFF and write harmonised one."""
        import rioxarray as rxr

        ds = _make_viirs_dataset()
        input_path = tmp_path / "input.tif"
        ds["flood_fraction"].rio.to_raster(str(input_path), dtype="uint8", compress="LZW", nodata=0)

        output_path = tmp_path / "output.tif"
        h = Harmoniser()
        result = h.harmonise_file(input_path, output_path, source_id="viirs")
        assert result == output_path
        assert output_path.exists()

        # Verify the output is a uint8 GeoTIFF at coarser resolution
        with rxr.open_rasterio(output_path) as da:
            assert da.dtype == np.uint8
            assert da.shape[-1] < 100  # coarser than input
            # Values should be in [0, 100] with 255 as nodata
            valid = da.values[da.values != 255]
            assert valid.max() <= 100

    def test_harmonise_empty_dataset(self):
        """Empty dataset should return empty."""
        import xarray as xr

        ds = xr.Dataset()
        h = Harmoniser()
        # Should raise: no spatial coords
        with pytest.raises((ValueError, KeyError)):
            h.harmonise(ds, source_id="viirs")

    def test_harmonise_no_classify(self):
        """Should handle datasets without quality_mask."""
        import rioxarray  # noqa: F401
        import xarray as xr

        res = 0.004
        w, n = 20.0, 35.4
        transform = from_bounds(w, n - 50 * res, w + 50 * res, n, 50, 50)

        ds = xr.Dataset(
            {"flood_fraction": xr.DataArray(np.zeros((50, 50), dtype=np.uint8), dims=["y", "x"])},
            coords={
                "x": w + (np.arange(50) + 0.5) * res,
                "y": n - (np.arange(50) + 0.5) * res,
            },
        )
        ds.rio.write_crs("EPSG:4326", inplace=True)
        ds.rio.write_transform(transform, inplace=True)

        h = Harmoniser()
        ds_out = h.harmonise(ds, source_id="viirs")
        assert "flood_fraction" in ds_out.data_vars
        assert "quality_mask" in ds_out.data_vars  # generated

    def test_harmonise_single_raster(self):
        """Single small raster should still produce valid output."""
        import rioxarray  # noqa: F401
        import xarray as xr

        res = 0.004
        w, n = 20.0, 35.4
        transform = from_bounds(w, n - 5 * res, w + 5 * res, n, 5, 5)

        ds = xr.Dataset(
            {"flood_fraction": xr.DataArray(np.ones((5, 5), dtype=np.uint8), dims=["y", "x"])},
            coords={
                "x": w + (np.arange(5) + 0.5) * res,
                "y": n - (np.arange(5) + 0.5) * res,
            },
        )
        ds.rio.write_crs("EPSG:4326", inplace=True)
        ds.rio.write_transform(transform, inplace=True)

        h = Harmoniser()
        ds_out = h.harmonise(ds, source_id="test")
        assert "flood_fraction" in ds_out.data_vars
        # Even small inputs should produce output
        assert ds_out["flood_fraction"].size > 0


# ── Tiler tests ──────────────────────────────────────────────────────────────


class TestTiler:
    def test_init_defaults(self):
        t = Tiler()
        assert t.tile_size == 224
        assert t.overlap == 0

    def test_init_custom(self):
        t = Tiler(tile_size=128, overlap=32)
        assert t.tile_size == 128
        assert t.overlap == 32

    def test_tile_size_must_be_positive(self):
        with pytest.raises(ValueError, match="tile_size must be positive"):
            Tiler(tile_size=0)

    def test_tile_size_must_be_positive_negative(self):
        with pytest.raises(ValueError, match="tile_size must be positive"):
            Tiler(tile_size=-10)

    def test_overlap_must_be_non_negative(self):
        with pytest.raises(ValueError, match="overlap must be non-negative"):
            Tiler(tile_size=224, overlap=-1)

    def test_tile_dataset_no_spatial_dims_raises(self):
        import xarray as xr

        t = Tiler()
        # Dataset with no recognised spatial dimension names
        ds = xr.Dataset({"flood_fraction": xr.DataArray(np.zeros((100, 100)))})
        with pytest.raises(ValueError, match="spatial dimensions"):
            t.tile_dataset(ds)

    def test_count_tiles_exact_multiple(self):
        import xarray as xr

        t = Tiler(tile_size=50)
        y = np.arange(100, dtype="float32")
        x = np.arange(200, dtype="float32")
        ds = xr.Dataset({"v": xr.DataArray(np.zeros((100, 200)), dims=["y", "x"], coords={"y": y, "x": x})})
        n_rows, n_cols = t.count_tiles(ds)
        assert n_rows == 2
        assert n_cols == 4

    def test_count_tiles_non_multiple(self):
        import xarray as xr

        t = Tiler(tile_size=60)
        y = np.arange(100, dtype="float32")
        x = np.arange(100, dtype="float32")
        ds = xr.Dataset({"v": xr.DataArray(np.zeros((100, 100)), dims=["y", "x"], coords={"y": y, "x": x})})
        n_rows, n_cols = t.count_tiles(ds)
        # ceil(100/60) = 2
        assert n_rows == 2
        assert n_cols == 2

    def test_tile_dataset_returns_correct_count(self):
        import xarray as xr

        t = Tiler(tile_size=50)
        y = np.arange(100, dtype="float32")
        x = np.arange(200, dtype="float32")
        ds = xr.Dataset({"v": xr.DataArray(np.zeros((100, 200)), dims=["y", "x"], coords={"y": y, "x": x})})
        tiles = t.tile_dataset(ds)
        # 2 rows × 4 cols
        assert len(tiles) == 8

    def test_tile_dataset_metadata_keys(self):
        import xarray as xr

        t = Tiler(tile_size=50)
        y = np.arange(100, dtype="float32")
        x = np.arange(100, dtype="float32")
        ds = xr.Dataset({"v": xr.DataArray(np.zeros((100, 100)), dims=["y", "x"], coords={"y": y, "x": x})})
        tiles = t.tile_dataset(ds)
        _, meta = tiles[0]
        assert set(meta.keys()) >= {"row", "col", "bbox", "valid_pixels"}

    def test_tile_dataset_covers_full_extent(self):
        """All pixels in the source dataset must appear in exactly one tile."""
        import xarray as xr

        tile_size = 32
        t = Tiler(tile_size=tile_size)
        h, w = 64, 96
        y = np.arange(h, dtype="float32")
        x = np.arange(w, dtype="float32")
        data = np.arange(h * w, dtype="float32").reshape(h, w)
        ds = xr.Dataset({"v": xr.DataArray(data, dims=["y", "x"], coords={"y": y, "x": x})})

        tiles = t.tile_dataset(ds)
        # Reconstruct from tiles
        reconstructed = np.full((h, w), np.nan)
        for tile_ds, meta in tiles:
            row, col = meta["row"], meta["col"]
            y_s = row * tile_size
            x_s = col * tile_size
            vals = tile_ds["v"].values
            reconstructed[y_s : y_s + vals.shape[0], x_s : x_s + vals.shape[1]] = vals

        np.testing.assert_array_equal(reconstructed, data)

    def test_get_tile_bbox_first_tile(self):
        import xarray as xr

        t = Tiler(tile_size=50)
        y = np.linspace(0.5, 49.5, 50)
        x = np.linspace(0.5, 49.5, 50)
        ds = xr.Dataset({"v": xr.DataArray(np.zeros((50, 50)), dims=["y", "x"], coords={"y": y, "x": x})})
        bbox = t.get_tile_bbox(0, 0, ds)
        assert len(bbox) == 4
        west, south, east, north = bbox
        assert west < east
        assert south < north

    def test_get_tile_bbox_out_of_bounds_raises(self):
        import xarray as xr

        t = Tiler(tile_size=50)
        y = np.arange(100, dtype="float32")
        x = np.arange(100, dtype="float32")
        ds = xr.Dataset({"v": xr.DataArray(np.zeros((100, 100)), dims=["y", "x"], coords={"y": y, "x": x})})
        with pytest.raises(IndexError):
            t.get_tile_bbox(99, 0, ds)

    def test_overlap_greater_than_tile_size_raises(self):
        with pytest.raises(ValueError, match="overlap must be less than tile_size"):
            Tiler(tile_size=32, overlap=32)
