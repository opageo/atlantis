"""Regression test for GFM's per-cell peak-memory fix (Phase C.3 of
``.github/prompts/plan-gfmPeakMemoryFix.prompt.md``).

Runs the real, unmodified ``GfmRasterProcessor.process_items`` classified
path against a small synthetic multi-band raster (a fake ``pystac``-like item
+ a monkeypatched ``odc.stac.load`` returning an in-memory ``xr.Dataset``
scaled down from the real ~15000x15000 native tile to 512x512), guarding
against two ways the Phase C.1/C.2 fix could regress:

1. **The split-load structure**: ``_load_item`` must be called *twice* per
   STAC item — once for the small "mask" band group, once for the "code"
   band group — never once for all 6 :data:`GFM_BANDS` together. Reverting to
   a single 6-band load was the single largest contributor to the measured
   ~15 GiB per-cell peak (see ``/memories/repo/gfm-investigation.md``).
2. **A proportionally-scaled peak-RSS ceiling**, as a coarse smoke check (this
   synthetic raster is ~1750x smaller than a real EQUI7 tile, so it can't
   reproduce the multi-gigabyte scale of the real bug — the call-count/band
   -grouping assertions above are the primary regression guard).
"""

from __future__ import annotations

import gc
import resource

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

        self.properties = {"proj:wkt2": pyproj.CRS.from_epsg(4326).to_wkt(), "gsd": 20.0}


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
