# GFM worker memory: root cause and per-item release fix

**Status:** implemented 2026-08-12 · applies to the Dask GFM cube batch
(`atlantis batch gfm cube run`) · GDAL 3.13 / rasterio 1.5 / distributed 2026.7

## TL;DR

The claim that "the VSICurl HTTP range-request cache accumulates to multi-GB"
is **not** what's happening. GDAL's two caches — the raster block cache
(`GDAL_CACHEMAX`, capped at 256 MB here) and the `/vsicurl/` region cache
(`CPL_VSIL_CURL_CACHE_SIZE`, hard-bounded at 16 MB in GDAL 3.13) — are both
LRU-bounded and stay flat when dataset handles are closed. The multi-GB
"unmanaged" RSS floor that killed 4 GB workers comes from:

1. **glibc heap fragmentation** — GDAL/curl allocate a stream of small
   (16 KB class) chunks interleaved with large buffers; freed small chunks are
   kept in the process heap / per-thread arenas and not returned to the OS,
   so the RSS _floor_ ratchets up even when nothing is leaked;
2. **retained dataset handles** — while a rasterio/GDAL dataset stays open it
   pins per-dataset native state _and_ its raster blocks in the block cache
   (the block cache only flushes fully when handles close);
3. **a cleanup cadence that ran once per task** — `harmonise_gfm_payload`
   flushed caches only at task end. Fine for production cells (1–3 items),
   not for 23-item tasks: the unmanaged floor then accumulates across every
   item of the task, and after Dask's 30-second unmanaged-memory grace period
   it counts against `pause` (80 % of `--memory-limit`) / nanny `terminate`
   (95 %), killing workers mid-task.

## Measured evidence

Production (operator measurement, 23-item tasks):

- worker RSS oscillates 1.3 ↔ 3.1 GB per item, but the baseline climbs to
  ~5.25 GB "unmanaged" within one task;
- `--memory-limit 4GB`: all tasks failed; `--memory-limit 8GB`: nanny
  restarts, tasks failed. With Dask's defaults this is `terminate = 0.95 ×
8 GB = 7.6 GB` firing on native (unspillable) memory: 5.25 GB baseline + a
  3.1 GB working-set spike overshoots it.

Controlled local-COG reproduction (`/tmp/kilo/repro_gfm_memory.py`, same
venv/GDAL 3.13, 23 items × 16 windows × 3 bands over `/vsicurl/` against a
local HTTP server):

| variant                              | RSS end            | glibc arena                       | block cache       | open handles            |
| ------------------------------------ | ------------------ | --------------------------------- | ----------------- | ----------------------- |
| control (handles closed each window) | **~101 MiB, flat** | 27 MiB                            | 0.0               | 0                       |
| devclean per item                    | ~92 MiB            | 18 MiB                            | 0.0               | 0 (+3 % range requests) |
| **leak** (handles never closed)      | **631 MiB**        | 556 MiB                           | **256 MiB (cap)** | 368                     |
| leak + devclean                      | 437 MiB            | **4.5 GB** (heap, mostly virtual) | 0.0               | 368                     |
| local disk (no HTTP)                 | ~96 MiB            | 25 MiB                            | 0.0               | 0                       |

Reads: with handles closed, the GDAL layer stays flat — no cleanup needed. The
only way to reproduce the GB-scale class of behaviour is to retain handles
(pins the block cache at its cap + per-dataset state) or to clear caches while
handles stay open (fragments the heap). `gdal.SetCacheMax(0)` +
`VSICurlClearCache()` remain useful (a real 6-cell A/B in
`scripts/profile_gfm_batch_rss.py` bounded RSS at ~330–380 MiB vs ~1.5 GiB
climbing), but their per-task-only cadence was the weak point.

## Changes

### 1 & 2 & 3b — per-item release cadence (the fix)

- `src/atlantis/fetchers/gfm/processor.py`
  - new `release_gdal_memory()`: `gc.collect()` → flush + restore the block
    cache (`gdal.SetCacheMax(0)` then back to `gdal.GetCacheMax()`),
    `gdal.VSICurlClearCache()`, then `_trim_malloc()`; best-effort, never
    affects pixel values (both caches are read caches).
  - `_trim_malloc()` moved here from `batch_processor.py` (glibc
    `malloc_trim(0)`, Linux-only).
  - called at the **end of every item** in `_process_items_classified` (and
    `_process_items_native`) — point 1 (drops xarray/rasterio dataset refs +
    `gc.collect()`), point 2 (flush cadence), point 3b (`malloc_trim`).
  - per-item (not per-window) deliberately: the `/vsicurl/` region cache is
    still allowed to absorb the shared boundary blocks _between the windows of
    the current item_ (see `gfm_optimize.md`).
- `src/atlantis/fetchers/gfm/batch_processor.py`
  - task-end cleanup replaced by a `release_gdal_memory()` call (belt-and-
    braces before the payload ships to the coordinator); `_trim_malloc` is
    re-exported for the profiling harness.
- `scripts/profile_gfm_batch_rss.py`
  - no-op patch updated to neutralise the centralized helper (patches
    `processor._trim_malloc` + `sys.modules["osgeo.gdal"]`).

### 4 — Dask budget sized around the unmanaged floor

- `src/atlantis/cli.py` (`batch gfm cube`): defaults changed from
  `--workers-max 3 --memory-limit 8GB` to `--workers-max 2 --memory-limit
12GB`. 2 × 12 GB = 24 GB fits a 32 GB host (with coordinator + OS), and
  `terminate = 0.95 × 12 GB = 11.4 GB` now covers the measured
  (5.25 + 3.1) GB peak. This is a deliberate **safety-over-throughput**
  trade: it fixes the nanny-restart failure mode but drops parallelism from 3
  to 2 workers.
- `batch_gfm_cube` now also **fails fast** before starting the cluster when
  `workers_max × memory_limit` exceeds ~80 % of the host's physical RAM
  (kernel-OOM kills would otherwise be silent because the run is detached in
  tmux).
- `docs/archive/cube-build.md`: GFM settings section and defaults table
  updated to match.

`MALLOC_ARENA_MAX` was intentionally **not** changed: with
`threads_per_worker=1` the per-thread arena ratchet is not the dominant driver
(reproduced: 4-thread control stayed flat), so it adds operational surface
without a measured win. Revisit only if GDAL_NUM_THREADS or concurrent
workers change.

## Validation / rollback

- Correctness: GFM output is byte-exact vs the reference — these changes only
  flush read caches and call `gc.collect()`/`malloc_trim`, which cannot change
  pixel values. Run `tests/fetchers/test_gfm_e2e.py` (strict reference bytes)
  and the windowed-correctness gate.
- Memory: `scripts/profile_gfm_batch_rss.py` (real EODC, cleanup on vs no-op)
  and `--workers-max 2 --memory-limit 12GB` on the 48-cell Africa-heavy 2025
  partition (previous gate: DONE=48, FAILED=0).
- Rollback: revert the `release_gdal_memory()` calls in `processor.py` to
  restore once-per-task cleanup; revert the CLI defaults to 3/8 GB. Each item
  is independently revertible.

## Deferred (deliberately not done)

- **Point 5 — splitting task granularity** (per-item Dask tasks). Deferred:
  it changes the multi-item accumulation semantics of
  `GfmRasterProcessor.process_items` (ascending + descending passes merge by
  pixel accumulation), which would break the byte-exact gate and require a
  per-cell reducer. The same benefits (retry blast radius, cadence alignment)
  come from the per-item release inside the existing task structure. Revisit
  only if 23-item retries/OOMs remain unacceptable after this fix.
- `MALLOC_ARENA_MAX`: see above.
