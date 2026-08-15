"""Multi-cell RSS harness for ``harmonise_gfm_payload`` — real EODC data, one process.

Unlike ``scripts/profile_gfm_peak_memory.py`` (single-cell *peak* RSS during
one ``GfmRasterProcessor.process_items()`` call), this harness answers a
different, narrower question raised while auditing the
``gdal.SetCacheMax(0)``/``VSICurlClearCache()``/``malloc_trim`` cleanup block
added to :func:`atlantis.fetchers.gfm.batch_processor.harmonise_gfm_payload`:
does that cleanup measurably reduce **cumulative** RSS *after* several
consecutive real cells finish in the same (Dask-worker-like) process, vs.
leaving it out?

Method: build several real ``(date, equi7_tile)`` tasks from the public EODC
STAC API (small bbox, ~1-2 items each), then call the real, unmodified
``harmonise_gfm_payload`` for each task in a loop within a single process,
sampling live ``VmRSS`` (from ``/proc/self/status`` — unlike
``ru_maxrss``/``resource``, this can go *down*, which matters for measuring
whether cleanup releases memory between cells) before and after each call.
Runs the whole loop twice: once with the module's cleanup block intact
(baseline), once with it monkeypatched to a no-op (``gdal.SetCacheMax`` /
``VSICurlClearCache`` replaced with no-ops, ``_trim_malloc`` replaced with a
no-op) — the delta between the two runs' final RSS is the cleanup's real,
measured effect, not a plausibility argument.

Usage (real EODC STAC network access required, no auth):

    PYTHONPATH=src python scripts/profile_gfm_batch_rss.py \
        --bbox -1.5 38.8 2.5 42.0 --start 2024-10-20 --end 2024-11-10 \
        --max-cells 6

**Result (2026-07-28, 6 real EU cells, isolated ``--pass`` A/B): the cleanup's
own effect is confirmed real at single-process scale, not just plausible.**
With it enabled, live VmRSS stayed bounded (~330-380 MiB) across all 6 cells;
with it monkeypatched to a no-op, VmRSS climbed to ~1.5 GiB (~4x higher,
still rising).

Note (2026-08): this harness measures the *aggregate* cleanup effect. It does
not by itself isolate which component does the work — and a controlled
local-COG reproduction (see docs/gfm/memory-root-cause.md) shows the GDAL
caches themselves (block cache + 16 MB ``/vsicurl/`` region cache) stay flat
once dataset handles are closed, so the residual climb comes from glibc heap
fragmentation plus retained dataset handles, which is why the durable fix is
the per-item release cadence (``release_gdal_memory``) added to
:mod:`atlantis.fetchers.gfm.processor`.

**This does NOT mean the real batch command is production-ready** — a real
multi-worker ``atlantis batch gfm cube run`` (default ``--workers-max 3
--memory-limit 8GB``) against an Africa-heavy catalogue partition failed
outright the same day (0 DONE / 37 FAILED, Dask's nanny killing/restarting
workers at ~95% memory budget) even with this fix in place. This harness
never exercises real ``LocalCluster``/``Nanny`` supervision or that
95%-restart threshold — treat it as a narrower diagnostic for the cleanup
block's own effect, not a substitute for testing the real batch command at
scale. See ``docs/archive/cube-build.md``'s "GFM inter-cell memory leak"
callout and GitHub issue #96 for the full, still-open investigation. Kept as
a reusable diagnostic script (not ``tmp/``) for re-verifying after any future
change to ``harmonise_gfm_payload``'s cleanup block — not part of the
permanent test suite (it needs real network access and takes several
minutes per pass).
"""

from __future__ import annotations

import argparse
import gc
import sys
import time
from collections import defaultdict
from datetime import datetime

from loguru import logger

sys.path.insert(0, "src")

from atlantis.fetchers.gfm.backend import DEFAULT_GFM_STAC_URL, GFM_COLLECTION_ID  # noqa: E402


def _vmrss_mib() -> float:
    """Current (live, can decrease) resident set size, in MiB. Linux-only."""
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    return -1.0


def _find_real_tasks(bbox: tuple[float, float, float, float], start: str, end: str, max_cells: int) -> list[dict]:
    """Group real EODC STAC items into ``(date, equi7_tile)`` task dicts.

    Mirrors :func:`atlantis.fetchers.gfm.inventory.to_tasks`'s grouping, but
    built directly from a live STAC search (no catalogue Parquet needed).
    """
    from pystac_client import Client

    catalog = Client.open(DEFAULT_GFM_STAC_URL)
    search = catalog.search(
        collections=GFM_COLLECTION_ID,
        bbox=bbox,
        datetime=(
            datetime.fromisoformat(start),
            datetime.fromisoformat(f"{end}T23:59:59"),
        ),
        max_items=200,
    )
    items = list(search.items())
    if not items:
        raise SystemExit(f"No GFM STAC items found for bbox={bbox} in [{start}, {end}] — widen the range.")

    groups: dict[tuple[str, str], list] = defaultdict(list)
    for item in items:
        tile = item.properties.get("Equi7Tile")
        if not tile or not item.bbox:
            continue
        day = item.datetime.date().isoformat()
        groups[(day, tile)].append(item)

    tasks: list[dict] = []
    for (day, tile), group_items in groups.items():
        if len(tasks) >= max_cells:
            break
        west = min(it.bbox[0] for it in group_items)
        south = min(it.bbox[1] for it in group_items)
        east = max(it.bbox[2] for it in group_items)
        north = max(it.bbox[3] for it in group_items)
        tasks.append(
            {
                "task_id": f"gfm-{day.replace('-', '')}-{tile}",
                "date": day,
                "equi7_tile": tile,
                "item_hrefs": [it.get_self_href() for it in group_items],
                "bbox": (west, south, east, north),
            }
        )
    return tasks


def _patch_cleanup_noop(monkeypatch_module) -> None:
    """Neutralise the GDAL/malloc cleanup (per-item + task-end) in place."""
    import atlantis.fetchers.gfm.processor as proc

    # Both the per-item cadence and batch_processor's task-end call run
    # through release_gdal_memory() in processor's module globals, so
    # patching proc._trim_malloc is what sticks.
    monkeypatch_module.setattr(proc, "_trim_malloc", lambda: None)

    class _NoopGdal:
        def GetCacheMax(self, *_a, **_kw):
            return 0

        def SetCacheMax(self, *_a, **_kw):
            pass

        def VSICurlClearCache(self):
            pass

    # release_gdal_memory() imports `from osgeo import gdal` *inside* the
    # function body, so patching sys.modules["osgeo.gdal"] is what's needed,
    # not an attribute on either module.
    import sys as _sys

    monkeypatch_module.setitem(_sys.modules, "osgeo.gdal", _NoopGdal())


class _FakeMonkeypatch:
    """Minimal stand-in for pytest's monkeypatch, usable outside a test."""

    def __init__(self) -> None:
        self._undo: list[tuple[str, object, object, bool]] = []

    def setattr(self, obj, name, value) -> None:
        had = hasattr(obj, name)
        old = getattr(obj, name, None)
        self._undo.append(("attr", obj, (name, old, had), None))
        setattr(obj, name, value)

    def setitem(self, mapping, key, value) -> None:
        had = key in mapping
        old = mapping.get(key)
        self._undo.append(("item", mapping, (key, old, had), None))
        mapping[key] = value

    def undo(self) -> None:
        for kind, target, payload, _ in reversed(self._undo):
            if kind == "attr":
                name, old, had = payload
                if had:
                    setattr(target, name, old)
                else:
                    delattr(target, name)
            else:
                key, old, had = payload
                if had:
                    target[key] = old
                else:
                    target.pop(key, None)
        self._undo.clear()


def run_pass(tasks: list[dict], label: str, cleanup_enabled: bool) -> list[float]:
    """Run ``harmonise_gfm_payload`` over every task once; return post-call VmRSS series."""
    from atlantis.fetchers.gfm.batch_processor import harmonise_gfm_payload

    mp = _FakeMonkeypatch()
    if not cleanup_enabled:
        _patch_cleanup_noop(mp)

    rss_series: list[float] = []
    print(f"\n=== Pass: {label} (cleanup_enabled={cleanup_enabled}) ===")
    print(f"{'cell':<45} {'wall(s)':>8} {'VmRSS before':>13} {'VmRSS after':>12} {'delta':>8}")
    try:
        for task in tasks:
            gc.collect()
            before = _vmrss_mib()
            t0 = time.perf_counter()
            try:
                harmonise_gfm_payload(task)
            except Exception as exc:  # noqa: BLE001 - keep profiling even if one cell fails
                print(f"{task['task_id']:<45}  FAILED: {exc!r}")
                continue
            elapsed = time.perf_counter() - t0
            gc.collect()
            after = _vmrss_mib()
            rss_series.append(after)
            print(f"{task['task_id']:<45} {elapsed:>8.1f} {before:>13.1f} {after:>12.1f} {after - before:>+8.1f}")
    finally:
        mp.undo()
    return rss_series


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bbox", nargs=4, type=float, default=[-1.5, 38.8, 2.5, 42.0])
    parser.add_argument("--start", default="2024-10-20")
    parser.add_argument("--end", default="2024-11-10")
    parser.add_argument("--max-cells", type=int, default=6)
    parser.add_argument(
        "--pass",
        dest="which_pass",
        choices=["enabled", "disabled"],
        default=None,
        help=(
            "Internal: run only one pass in this process (used by the "
            "subprocess-isolated wrapper so each pass starts from a clean "
            "interpreter, matching a real per-task-batch Dask worker "
            "lifetime, instead of two passes sharing one already-warmed-up "
            "process). Omit to run both passes back-to-back in one process "
            "(faster, but the second pass's baseline is already inflated by "
            "the first — use --pass for a clean A/B comparison)."
        ),
    )
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    tasks = _find_real_tasks(tuple(args.bbox), args.start, args.end, args.max_cells)
    print(f"Found {len(tasks)} real (date, equi7_tile) cells to replay:")
    for t in tasks:
        print(f"  {t['task_id']}  items={len(t['item_hrefs'])}")

    if args.which_pass == "enabled":
        run_pass(tasks, "cleanup ENABLED (current code, as shipped)", cleanup_enabled=True)
        return
    if args.which_pass == "disabled":
        run_pass(tasks, "cleanup DISABLED (monkeypatched no-op)", cleanup_enabled=False)
        return

    baseline = run_pass(tasks, "cleanup ENABLED (current code, as shipped)", cleanup_enabled=True)
    noop = run_pass(tasks, "cleanup DISABLED (monkeypatched no-op)", cleanup_enabled=False)

    print("\n=== Summary (same-process, second pass baseline already warmed up — see --pass for isolated A/B) ===")
    if baseline:
        print(f"cleanup ENABLED : final VmRSS={baseline[-1]:.1f} MiB  peak={max(baseline):.1f} MiB")
    if noop:
        print(f"cleanup DISABLED: final VmRSS={noop[-1]:.1f} MiB  peak={max(noop):.1f} MiB")
    if baseline and noop:
        print(f"Delta (disabled - enabled) at final cell: {noop[-1] - baseline[-1]:+.1f} MiB")
        print(f"Delta (disabled - enabled) at peak:        {max(noop) - max(baseline):+.1f} MiB")


if __name__ == "__main__":
    main()
