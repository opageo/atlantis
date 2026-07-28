"""Regression test for GFM's per-cell peak-memory fix.

Runs the real, unmodified ``GfmRasterProcessor.process_items`` classified
path against a small synthetic multi-band raster (a fake ``pystac``-like item
+ a monkeypatched ``odc.stac.load`` returning an in-memory ``xr.Dataset``
scaled down from the real ~15000x15000 native tile to 512x512), guarding
against two ways the fix could regress:

1. **The split-load structure**: ``_load_item`` must be called *twice* per
   STAC item — once for the small "mask" band group, once for the "code"
   band group — never once for all 6 :data:`GFM_BANDS` together. Reverting to
   a single 6-band load was the single largest contributor to the measured
   ~15 GiB per-cell peak.
2. **A proportionally-scaled peak-RSS ceiling**, as a coarse smoke check (this
   synthetic raster is ~1750x smaller than a real EQUI7 tile, so it can't
   reproduce the multi-gigabyte scale of the real bug — the call-count/band
   -grouping assertions above are the primary regression guard).
"""

from __future__ import annotations

import gc
import resource
from types import SimpleNamespace

import numpy as np
import pytest
import xarray as xr

from atlantis.fetchers.gfm import processor as gfm_processor_module
from atlantis.fetchers.gfm.processor import GfmRasterProcessor

_SIZE = 512
_BBOX = (10.0, 20.0, 10.5, 20.5)


class _FakeItem:
    """Minimal stand-in for a ``pystac.Item`` — only what the processor reads."""

    def __init__(self, item_id: str = "fake-item") -> None:
        self.id = item_id
        # A plain lon/lat CRS keeps this test independent of any real Equi7
        # projection — the processor only needs a CRS pyproj can parse and
        # rioxarray can reproject *from*, not a specific one.
        import pyproj

        self.properties = {
            "proj:wkt2": pyproj.CRS.from_epsg(4326).to_wkt(),
            "gsd": 20.0,
            # proj:shape/proj:transform are required by the windowed path
            # (_native_pixel_windows derives window geometry from them).
            # _SIZE x _SIZE grid, pixel size 20m, origin at (_BBOX[0], _BBOX[3]).
            "proj:shape": [_SIZE, _SIZE],
            "proj:transform": [20.0, 0.0, _BBOX[0], 0.0, -20.0, _BBOX[3]],
        }


def _synthetic_bands() -> dict[str, xr.DataArray]:
    """Build all 6 GFM_BANDS at a small resolution, spanning `_BBOX`."""
    rng = np.random.default_rng(42)
    y = np.linspace(_BBOX[3], _BBOX[1], _SIZE)
    x = np.linspace(_BBOX[0], _BBOX[2], _SIZE)
    coords = {"y": y, "x": x}

    def _band(values: np.ndarray) -> xr.DataArray:
        return xr.DataArray(values[np.newaxis, :, :], dims=("time", "y", "x"), coords={"time": [0], **coords})

    return {
        "ensemble_flood_extent": _band(rng.integers(0, 2, size=(_SIZE, _SIZE)).astype("uint8")),
        "ensemble_water_extent": _band(rng.integers(0, 2, size=(_SIZE, _SIZE)).astype("uint8")),
        "reference_water_mask": _band(np.zeros((_SIZE, _SIZE), dtype="uint8")),
        "exclusion_mask": _band(np.zeros((_SIZE, _SIZE), dtype="uint8")),
        "ensemble_likelihood": _band(rng.integers(0, 101, size=(_SIZE, _SIZE)).astype("uint8")),
        "advisory_flags": _band(np.zeros((_SIZE, _SIZE), dtype="uint8")),
    }


@pytest.fixture()
def fake_odc_load(monkeypatch):
    """Patch ``odc.stac.load`` to return a scaled-down synthetic Dataset.

    Records the ``bands`` requested on every call so tests can assert the
    load was split into two band groups instead of one 6-band call.
    """
    import odc.stac

    all_bands = _synthetic_bands()
    calls: list[list[str]] = []

    def _fake_load(_items, *, bands, **_kwargs):
        calls.append(list(bands))
        return xr.Dataset({name: all_bands[name] for name in bands})

    monkeypatch.setattr(odc.stac, "load", _fake_load)
    return calls


def test_load_is_split_into_two_band_groups(fake_odc_load):
    """`_load_item` must be called twice per item — never once for all 6 bands.

    This is the direct regression guard for the Phase C.2 fix: reverting to a
    single ``odc.stac.load(bands=GFM_BANDS, ...)`` call reintroduces the
    dominant contributor to the measured ~15 GiB per-cell peak.
    """
    processor = GfmRasterProcessor(bbox=_BBOX, coarsen_factor=4, classify=True)

    result = processor.process_items(
        [_FakeItem()],
        event_id="",
        date_token="test",
        output_dir=None,
        write_outputs=False,
    )

    assert result is not None
    assert len(fake_odc_load) == 2, f"expected 2 odc.stac.load calls (mask group + code group), got {fake_odc_load}"
    assert set(fake_odc_load[0]) == set(gfm_processor_module._CLASSIFIED_MASK_BANDS)
    assert set(fake_odc_load[1]) == set(gfm_processor_module._CLASSIFIED_CODE_BANDS)
    # No band should ever be requested in both groups (no duplicate fetch) or
    # neither group (a leftover GFM_BANDS entry orphaned by a future edit).
    all_native_bands = set(gfm_processor_module.GFM_BANDS)
    assert set(fake_odc_load[0]) | set(fake_odc_load[1]) == all_native_bands
    assert set(fake_odc_load[0]) & set(fake_odc_load[1]) == set()


def test_load_item_uses_synchronous_inner_scheduler(monkeypatch):
    """Nested odc/xarray loading must stay inside the outer Dask worker."""
    import odc.stac

    loaded_with: list[object] = []
    dataset = xr.Dataset()

    def _load(*_args, **_kwargs):
        return dataset

    original_load = xr.Dataset.load

    def _record_scheduler(self, *args, **kwargs):
        loaded_with.append(kwargs.get("scheduler"))
        return original_load(self, *args, **kwargs)

    monkeypatch.setattr(odc.stac, "load", _load)
    monkeypatch.setattr(xr.Dataset, "load", _record_scheduler)

    processor = GfmRasterProcessor(bbox=_BBOX)
    aoi = SimpleNamespace(bounds=_BBOX)
    assert processor._load_item(_FakeItem(), aoi, "EPSG:4326", 20.0, bands=["ensemble_flood_extent"]) is dataset
    assert loaded_with == ["synchronous"]


def test_process_items_peak_rss_within_scaled_bound(fake_odc_load):
    """Coarse smoke check: peak RSS growth stays within a generous bound.

    This synthetic raster (512x512) is ~1750x smaller than a real EQUI7 tile
    (~15000x15000), so it cannot reproduce the multi-gigabyte scale of the
    real bug — ``test_load_is_split_into_two_band_groups`` above is the
    primary regression guard. This assertion is a secondary, coarse ceiling
    only (Linux/macOS; ``ru_maxrss`` is the OS-level RSS high-water mark, the
    same metric ``distributed.worker.memory`` uses, and it captures native
    GDAL/numpy buffers a pure ``tracemalloc`` run would miss).
    """
    processor = GfmRasterProcessor(bbox=_BBOX, coarsen_factor=4, classify=True)

    gc.collect()
    before_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    result = processor.process_items(
        [_FakeItem()],
        event_id="",
        date_token="test",
        output_dir=None,
        write_outputs=False,
    )

    gc.collect()
    after_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    delta_mib = (after_kib - before_kib) / 1024.0

    assert result is not None
    # Generous ceiling for a 512x512 tile — real per-cell peaks at full scale
    # are gigabytes; this only guards against a gross regression at this
    # tiny scale (e.g. accidentally loading and retaining many full-size
    # duplicate copies of the synthetic bands).
    assert delta_mib < 500, f"peak RSS grew {delta_mib:.1f} MiB processing a 512x512 synthetic tile"


def test_windowed_matches_unwindowed_within_tolerance(monkeypatch):
    """Windowed output must match unwindowed within 1e-5 on both fields.

    Uses a 1024x1024 EPSG:3857 synthetic raster (proj:transform pixel size
    must match coordinate units for the windowed path's geometry math) with
    window_size=256 (4x4 grid).
    """
    import odc.stac

    size = 1024
    coarsen_factor = 4
    window_size = 256

    rng = np.random.default_rng(42)
    import pyproj

    crs_wkt = pyproj.CRS.from_epsg(3857).to_wkt()
    west_m, north_m, pixel_size = 1.1e6, 2.6e6, 20.0
    y = north_m - (np.arange(size) + 0.5) * pixel_size
    x = west_m + (np.arange(size) + 0.5) * pixel_size
    coords = {"y": y, "x": x}
    _t = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    _w, _n = _t.transform(west_m, north_m)
    _e, _s = _t.transform(west_m + size * pixel_size, north_m - size * pixel_size)
    bbox = (_w, _s, _e, _n)

    def _band(values: np.ndarray) -> xr.DataArray:
        return xr.DataArray(values[np.newaxis, :, :], dims=("time", "y", "x"), coords={"time": [0], **coords})

    all_bands = {
        "ensemble_flood_extent": _band(rng.integers(0, 2, size=(size, size)).astype("uint8")),
        "ensemble_water_extent": _band(rng.integers(0, 2, size=(size, size)).astype("uint8")),
        "reference_water_mask": _band(np.zeros((size, size), dtype="uint8")),
        "exclusion_mask": _band(np.zeros((size, size), dtype="uint8")),
        "ensemble_likelihood": _band(rng.integers(0, 101, size=(size, size)).astype("uint8")),
        "advisory_flags": _band(np.zeros((size, size), dtype="uint8")),
    }

    fake_item = _FakeItem()
    fake_item.properties = {
        "proj:wkt2": crs_wkt,
        "gsd": 20.0,
        "proj:shape": [size, size],
        "proj:transform": [pixel_size, 0.0, west_m, 0.0, -pixel_size, north_m],
    }

    calls: list[list[str]] = []

    def _fake_load(_items, *, bands, **_kwargs):
        calls.append(list(bands))
        return xr.Dataset({name: all_bands[name] for name in bands})

    monkeypatch.setattr(odc.stac, "load", _fake_load)

    # Unwindowed reference
    ref_processor = GfmRasterProcessor(bbox=bbox, coarsen_factor=coarsen_factor, classify=True)
    ref_result = ref_processor.process_items(
        [fake_item],
        event_id="",
        date_token="ref",
        output_dir=None,
        write_outputs=False,
    )
    assert ref_result is not None

    # Windowed
    calls.clear()
    win_processor = GfmRasterProcessor(bbox=bbox, coarsen_factor=coarsen_factor, classify=True, window_size=window_size)
    win_result = win_processor.process_items(
        [fake_item],
        event_id="",
        date_token="win",
        output_dir=None,
        write_outputs=False,
    )
    assert win_result is not None

    # Windowed path must make >2 load calls (windows × band groups).
    assert len(calls) > 2, f"windowed path should make >2 load calls, got {len(calls)}"

    for field in ("flood_fraction", "water_fraction"):
        ref_arr = getattr(ref_result.processed, field)
        win_arr = getattr(win_result.processed, field)
        assert ref_arr.shape == win_arr.shape, f"{field}: shape mismatch {ref_arr.shape} vs {win_arr.shape}"
        finite = np.isfinite(ref_arr) & np.isfinite(win_arr)
        diff = np.abs(ref_arr[finite] - win_arr[finite])
        max_diff = float(diff.max()) if diff.size else 0.0
        assert max_diff <= 1e-5, f"{field}: windowed vs unwindowed max diff {max_diff:.3e} exceeds 1e-5"
