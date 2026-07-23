"""Unit tests for viirs/batch_processor.py — offline, fixture-based."""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds


def _make_fixture_tif(tmp_path, rows=200, cols=200) -> str:
    """Write a synthetic VIIRS-coded uint8 GeoTIFF and return its path."""
    data = np.full((rows, cols), 17, dtype=np.uint8)
    # Flood fraction codes: 101–200 → fraction (code-100)/100
    data[10:50, 10:50] = 150  # 50% flood fraction
    data[60:80, 60:80] = 30  # cloud
    data[90:100, 90:100] = 17  # permanent water

    transform = from_bounds(-10.0, -5.0, 0.0, 5.0, cols, rows)
    path = str(tmp_path / "fixture.tif")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(data, 1)
    return path


def test_classify_viirs_pixels_flood_fraction():
    from atlantis.fetchers.viirs.processor import classify_viirs_pixels

    rows, cols = 100, 100
    # Background is vegetation (17), which is now excluded (NaN) rather than
    # a confirmed non-flood observation — see test_layers.py for the rationale.
    data = np.full((rows, cols), 17, dtype=np.uint8)
    data[0:50, 0:50] = 150  # code 150 → 50% flood
    transform = from_bounds(-10.0, -5.0, 0.0, 5.0, cols, rows)

    result = classify_viirs_pixels(data, transform, "EPSG:4326")

    flooded = result.flood_fraction[0:50, 0:50]
    assert np.allclose(flooded, 0.5, atol=0.01)
    not_flooded = result.flood_fraction[50:, 50:]
    assert np.all(np.isnan(not_flooded))


def test_classify_viirs_pixels_nodata_fill():
    from atlantis.fetchers.viirs.processor import FILL_CODES, classify_viirs_pixels

    rows, cols = 10, 10
    data = np.full((rows, cols), list(FILL_CODES)[0], dtype=np.uint8)
    transform = from_bounds(-10.0, -5.0, 0.0, 5.0, cols, rows)

    result = classify_viirs_pixels(data, transform, "EPSG:4326")
    assert np.isnan(result.flood_fraction).all()
    assert np.all(result.exclusion_mask == 1)
    assert result.cloud_fraction == 0.0


@pytest.mark.e2e
def test_process_granule_cog_roundtrip(tmp_path, monkeypatch):
    """Run process_granule against a local fixture TIF, intercept the S3 upload,
    and verify the in-memory COG is valid."""
    fixture_path = _make_fixture_tif(tmp_path)

    captured: dict = {}

    class _FakeFS:
        def open(self, uri, mode):
            import io

            buf = io.BytesIO()
            captured["uri"] = uri

            class _Ctx:
                def __enter__(self_inner):
                    return buf

                def __exit__(self_inner, *a):
                    captured["bytes"] = buf.getvalue()

            return _Ctx()

    def _fake_s3fs():
        return _FakeFS()

    def _fake_download(url, suffix=".tif"):
        # In tests, the "url" is actually a local fixture path — copy it
        # so the process_granule contract (always operates on a tempfile
        # it owns and can unlink) is preserved.
        import shutil
        import tempfile
        from pathlib import Path

        fd, tmp_p = tempfile.mkstemp(suffix=suffix, prefix="viirs_test_")
        import os as _os

        _os.close(fd)
        shutil.copy(url, tmp_p)
        return Path(tmp_p)

    import atlantis.fetchers.viirs.batch_processor as bp

    monkeypatch.setattr(bp, "_s3fs_filesystem", _fake_s3fs)
    monkeypatch.setattr(bp, "_download_to_tempfile", _fake_download)

    task = {
        "task_id": "fixture_granule",
        "source_uri": fixture_path,
        "dest_key": "viirs/jpss/2020/2020-01-01/GLB001.tif",
        "date": "2020-01-01",
        "aoi_id": 1,
    }

    result = bp.process_granule(task)

    assert result.task_id == "fixture_granule"
    assert result.status == "DONE"
    assert "bytes" in captured

    # Validate the written bytes are a valid raster.
    with MemoryFile(captured["bytes"]) as mem:
        with mem.open() as ds:
            assert ds.dtypes[0] == "uint8"
            assert ds.nodata == 255
            arr = ds.read(1)
            assert arr.min() >= 0
            assert arr.max() <= 100 or arr.max() == 255


def test_harmonise_granule_payload_returns_full_mask_set(tmp_path, monkeypatch):
    """``harmonise_granule_payload`` must emit every non-``flood_fraction`` VIIRS derived layer."""
    fixture_path = _make_fixture_tif(tmp_path)

    def _fake_download(url, suffix=".tif"):
        import shutil
        import tempfile
        from pathlib import Path

        fd, tmp_p = tempfile.mkstemp(suffix=suffix, prefix="viirs_test_")
        import os as _os

        _os.close(fd)
        shutil.copy(url, tmp_p)
        return Path(tmp_p)

    import atlantis.fetchers.viirs.batch_processor as bp

    monkeypatch.setattr(bp, "_download_to_tempfile", _fake_download)

    task = {
        "task_id": "fixture_granule",
        "source_uri": fixture_path,
        "dest_key": "viirs/jpss/2020/2020-01-01/GLB001.tif",
        "date": "2020-01-01",
        "aoi_id": 1,
    }

    payload = bp.harmonise_granule_payload(task)

    expected = {
        "water_fraction",
        "exclusion_mask",
        "reference_water",
        "cloud_mask",
        "snow_ice",
        "shadow",
        "y",
        "x",
        "task_id",
        "date",
        "aoi_id",
        "dest_key",
    }
    assert expected.issubset(payload.keys())

    water = payload["water_fraction"]
    for key in ("exclusion_mask", "reference_water", "cloud_mask", "snow_ice", "shadow"):
        arr = payload[key]
        assert arr.shape == water.shape
        assert arr.dtype == np.uint8
        assert set(np.unique(arr).tolist()).issubset({0, 1, 255})
