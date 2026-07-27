"""Ad-hoc peak-memory profiling harness for one real GFM cell.

Calls the real, unmodified :meth:`GfmRasterProcessor.process_items` (the exact
path ``harmonise_gfm_payload`` uses in production) for a single real STAC item,
transparently wrapping ``_load_item`` and ``_build_native_masks`` — the two
stages identified as responsible for 100% of the measured ~15 GiB peak — to
print the RSS high-water mark after each call, and counting how many times
``_load_item`` is actually called (1 for the unwindowed path, N windows for
the windowed path) plus total wall-clock time.

Samples ``resource.getrusage(RUSAGE_SELF).ru_maxrss`` (the OS-level RSS
high-water mark — this is what ``distributed.worker.memory`` also keys off
of, and it captures native GDAL/numpy buffers that a pure ``tracemalloc`` run
would miss). ``ru_maxrss`` never decreases within a process, so each printed
delta is the amount by which the process-wide high-water mark grew *during*
that call — the right tool for attributing a peak that includes
non-Python-tracked native allocations, and zero-dependency (stdlib
``resource``, Linux/macOS only).

Usage (network access to the public EODC STAC API required, no auth):

    PYTHONPATH=src python scripts/profile_gfm_peak_memory.py \
        --bbox -1.5 38.8 0.5 40.0 \
        --start 2024-10-29 --end 2024-11-04 \
        --window-size 3000

Omit ``--window-size`` (or pass nothing) to profile the unwindowed
(``window_size=None``) path — the pre-Phase-W baseline.

This is a reusable diagnostic/profiling script (kept in ``scripts/``, not
``tmp/``, precisely so it stays available across sessions and clones — it's
referenced by name in multiple plan documents), not part of the permanent
test suite (see ``tests/fetchers/gfm/test_processor_memory.py`` for the small
synthetic regression test added in Phase C.3).
"""

from __future__ import annotations

import argparse
import functools
import gc
import resource
import sys
import time
from datetime import datetime

from loguru import logger

sys.path.insert(0, "src")

from atlantis.fetchers.gfm.backend import DEFAULT_GFM_STAC_URL, GFM_COLLECTION_ID  # noqa: E402
from atlantis.fetchers.gfm.processor import GfmRasterProcessor  # noqa: E402


def _rss_mib() -> float:
    """Current process RSS high-water mark, in MiB (Linux: ru_maxrss is KiB)."""
    kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return kib / 1024.0


class _StageTimer:
    """Prints the RSS high-water-mark delta since the previous checkpoint."""

    def __init__(self) -> None:
        self._last = _rss_mib()
        self._start = self._last
        print(f"{'stage':<55} {'peak RSS (MiB)':>16} {'delta (MiB)':>14}")
        print("-" * 88)

    def mark(self, label: str) -> None:
        gc.collect()  # reclaim Python-tracked garbage so the delta isn't inflated by it
        current = _rss_mib()
        delta = current - self._last
        print(f"{label:<55} {current:>16.1f} {delta:>+14.1f}")
        self._last = current

    def summary(self) -> None:
        print("-" * 88)
        print(f"{'TOTAL since start':<55} {self._last:>16.1f} {self._last - self._start:>+14.1f}")


def _find_real_item(bbox: tuple[float, float, float, float], start: str, end: str):
    """Search the public EODC STAC API for one real GFM item covering *bbox*."""
    from pystac_client import Client

    catalog = Client.open(DEFAULT_GFM_STAC_URL)
    search = catalog.search(
        collections=GFM_COLLECTION_ID,
        bbox=bbox,
        datetime=(
            datetime.fromisoformat(start),
            datetime.fromisoformat(f"{end}T23:59:59"),
        ),
        max_items=5,
    )
    items = list(search.items())
    if not items:
        raise SystemExit(f"No GFM STAC items found for bbox={bbox} in [{start}, {end}] — widen the date range or bbox.")
    for item in items:
        if item.properties.get("Equi7Tile") and item.bbox:
            return item
    raise SystemExit("Found items but none had an Equi7Tile property + bbox.")


def _instrument(processor: GfmRasterProcessor, timer: _StageTimer) -> dict[str, int]:
    """Wrap the two Phase-C.1-identified hotspot methods with RSS checkpoints.

    Returns a mutable counters dict (``{"load_item_calls": n}``) updated in
    place — the request-count trade-off is needed, not just memory.
    """
    orig_load_item = processor._load_item
    orig_build_masks = processor._build_native_masks
    counters = {"load_item_calls": 0}

    @functools.wraps(orig_load_item)
    def _load_item_traced(item, aoi, crs_src, resolution, *, bands=None):
        counters["load_item_calls"] += 1
        result = orig_load_item(item, aoi, crs_src, resolution, bands=bands)
        timer.mark(f"_load_item(bands={bands}) call #{counters['load_item_calls']}")
        return result

    @functools.wraps(orig_build_masks)
    def _build_native_masks_traced(*args, **kwargs):
        result = orig_build_masks(*args, **kwargs)
        timer.mark("_build_native_masks")
        return result

    processor._load_item = _load_item_traced
    processor._build_native_masks = _build_native_masks_traced
    return counters


def profile_one_cell(
    bbox_query: tuple[float, float, float, float], start: str, end: str, window_size: int | None
) -> None:
    item = _find_real_item(bbox_query, start, end)
    tile_bbox = tuple(item.bbox)
    print(f"Using real STAC item: {item.id}")
    print(f"  Equi7Tile: {item.properties.get('Equi7Tile')}")
    print(f"  item.bbox (== EQUI7 tile bbox): {tile_bbox}")
    print(f"  gsd: {item.properties.get('gsd')}")
    print(f"  window_size: {window_size!r}\n")

    processor = GfmRasterProcessor(bbox=tile_bbox, coarsen_factor=4, classify=True, window_size=window_size)

    timer = _StageTimer()
    timer.mark("baseline (before any GFM work)")
    counters = _instrument(processor, timer)

    wall_start = time.perf_counter()
    result = processor.process_items(
        [item],
        event_id="",
        date_token="profile",
        output_dir=None,
        write_outputs=False,
    )
    wall_elapsed = time.perf_counter() - wall_start
    timer.mark("process_items() returned")
    timer.summary()

    print(f"\nWall-clock time for process_items(): {wall_elapsed:.1f}s")
    print(f"_load_item() call count: {counters['load_item_calls']}")

    if result is None:
        print("\nprocess_items() returned None (no valid data) — check item validity.")
        return
    print(f"Output shape: {result.processed.flood_fraction.shape}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bbox", nargs=4, type=float, default=[-1.5, 38.8, 0.5, 40.0])
    parser.add_argument("--start", default="2024-10-29")
    parser.add_argument("--end", default="2024-11-04")
    parser.add_argument(
        "--window-size",
        type=int,
        default=None,
        help="Native pixels per window (classified path only). Omit for the unwindowed baseline.",
    )
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    profile_one_cell(tuple(args.bbox), args.start, args.end, args.window_size)
