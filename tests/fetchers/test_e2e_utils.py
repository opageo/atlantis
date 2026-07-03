from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from upath import UPath

from tests.fetchers._e2e_utils import STRICT_REFERENCE_BYTES_ENV, compare_rasters, strict_reference_bytes_enabled


def _write_raster(path: Path, data: np.ndarray, *, compress: str | None = None) -> None:
    height, width = data.shape
    kwargs = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "uint8",
        "crs": "EPSG:4326",
        "transform": from_origin(0, height, 1, 1),
        "nodata": 0,
    }
    if compress is not None:
        kwargs["compress"] = compress

    with rasterio.open(path, "w", **kwargs) as dst:
        dst.write(data, 1)


def test_strict_reference_bytes_enabled_parses_truthy_values(monkeypatch):
    monkeypatch.setenv(STRICT_REFERENCE_BYTES_ENV, "ON")
    assert strict_reference_bytes_enabled() is True

    monkeypatch.setenv(STRICT_REFERENCE_BYTES_ENV, "0")
    assert strict_reference_bytes_enabled() is False


def test_compare_rasters_strict_mode_rejects_byte_drift(tmp_path):
    data = np.array([[0, 1], [2, 3]], dtype=np.uint8)
    produced = tmp_path / "produced.tif"
    reference = tmp_path / "reference.tif"

    _write_raster(produced, data)
    _write_raster(reference, data, compress="LZW")

    compare_rasters(UPath(produced), UPath(reference))

    with pytest.raises(AssertionError, match="Exact reference"):
        compare_rasters(UPath(produced), UPath(reference), require_byte_identity=True)


def test_compare_rasters_strict_mode_accepts_identical_files(tmp_path):
    data = np.array([[1, 2], [3, 4]], dtype=np.uint8)
    produced = tmp_path / "produced.tif"
    reference = tmp_path / "reference.tif"

    _write_raster(produced, data)
    reference.write_bytes(produced.read_bytes())

    compare_rasters(UPath(produced), UPath(reference), require_byte_identity=True)
