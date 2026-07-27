# Plan: Fix GFM Dask worker-memory warnings, then resume from checkpoint

## TL;DR

The `distributed.worker.memory` warnings ("Unmanaged memory... high", "Pausing
worker") during `atlantis batch gfm cube run` are almost certainly GDAL's
per-process raster block cache growing unbounded across every distinct COG
opened over a worker's lifetime (no `GDAL_CACHEMAX` bound is set anywhere in
the GFM path — MODIS/VIIRS only set `GDAL_NUM_THREADS`, not a cache bound
either), compounded by glibc not returning freed numpy/GDAL buffers back to
the OS after each task (`gc.collect()` already runs in `harmonise_gfm_payload`
but can't reclaim non-Python/native allocations). Each GFM task can
transiently need multiple GB (up to 6 bands x 15000x15000 px per STAC item;
docs/archive/cube-build.md already flags GFM as needing "VIIRS-level
headroom") which leaves little room under the default 4GB/worker limit. Fix =
bound `GDAL_CACHEMAX` + force `malloc_trim(0)` after `gc.collect()` in
`src/atlantis/fetchers/gfm/batch_processor.py`, then validate with a small,
disposable test run before resuming the real 2025 backlog from the
S3-uploaded tracker DB (`s3://atlantis/db/archive_tracker_gfm_2025.db`).

User confirmed: keep the fix minimal (no worker-lifetime-recycling safety
net), exact original `--archive` root / partition / RAM are NOT known for
certain — plan includes a discovery step instead of guessing.

## Steps

### Phase 1 — Code fix (no dependencies)

1. Edit `src/atlantis/fetchers/gfm/batch_processor.py`:
   - Add `import os`.
   - Add module-level `os.environ.setdefault("GDAL_CACHEMAX", "256")` (MB),
     placed the same way `GDAL_NUM_THREADS` is set in
     `src/atlantis/fetchers/modis/batch_processor.py` (L55) and
     `src/atlantis/fetchers/viirs/batch_processor.py` (L56) — this executes
     once per worker process on first import (Dask unpickles the module-level
     `harmonise_gfm_payload` reference, importing the module before the task
     body runs), before any `odc.stac.load()` call, so it takes effect for
     every raster read in that worker.
2. Same file: add a small `_trim_malloc()` helper (local `import ctypes`,
   `sys.platform.startswith("linux")` guard, `ctypes.CDLL("libc.so.6").malloc_trim(0)`
   wrapped in try/except OSError) and call it immediately after the existing
   `gc.collect()` call at the end of `harmonise_gfm_payload` (currently the
   line right before the `logger.debug("gfm cell {} {} → shape {}", ...)` /
   `return {...}`). `gc.collect()` only frees Python-tracked references —
   this is the standard mitigation the warning's own linked doc
   (distributed.dask.org .../worker-memory.html#memory-not-released-back-to-the-os)
   recommends for memory that isn't released back to the OS.
   - Both edits are additive/independent — can be done in one pass.

### Phase 2 — Retry from checkpoint _(depends on Phase 1)_

**2.1 Discover & confirm the exact original run coordinates** — the tracker
DB does not record which `--archive` it was writing into, and the exact
original `--partition`/flags are unconfirmed, so verify rather than guess:

- Check host RAM (`free -h`) to pick a tier from the table below.
- List candidates: `aws s3 ls s3://atlantis/zarr/ --endpoint-url https://object-store.os-api.cci1.ecmwf.int`
  and `aws s3 ls s3://atlantis/assets/gfm/ --endpoint-url https://object-store.os-api.cci1.ecmwf.int`
  (or reuse `scripts/s3_size_info.sh 2025`, `scripts/s3_size_info.sh gfm_cube`,
  etc. — its own usage examples already reference a `2025` store name).
  Look for the yearly GFM catalogue (matches the documented year-chunked
  convention in `docs/archive/cube-build.md` §3.2, e.g.
  `gfm_archive_catalog_2025.parquet`) and a candidate cube root (e.g.
  `s3://atlantis/zarr/2025`; CLI default is `s3://atlantis/zarr/gfm_cube`).
- Download the tracker: `aws s3 cp s3://atlantis/db/archive_tracker_gfm_2025.db
./gfm_cube_tracker_2025.db --endpoint-url https://object-store.os-api.cci1.ecmwf.int`.
- Confirm the guess (no risk, read-only): `atlantis batch gfm cube status
--db-path ./gfm_cube_tracker_2025.db --inventory <candidate-catalogue>` —
  a plausible non-zero `DONE` count strictly less than `total` confirms the
  right catalogue. Cross-check the candidate `--archive` actually has a
  `gfm` Zarr group already (`aws s3 ls <candidate-archive>/datacube.zarr/gfm/ ...`).

**2.2 Small, disposable test** _(depends on 2.1; validates Phase 1 before
touching the real backlog)_:

- Run `atlantis batch gfm cube run` against a tiny `--partition` slice
  (e.g. `0:30`) of the SAME confirmed catalogue, but pointed at a
  throwaway `--archive` (e.g. `./tmp_gfm_test_cube`) and a fresh
  `--db-path` (e.g. `./gfm_test_tracker.db`) — never the real
  tracker/archive.
- Use the RAM-appropriate `--workers-min/--workers-max/--memory-limit`
  from the table below; run in the foreground first (not detached) so
  logs and the dask dashboard (`--dashboard-port`, default 8789) are
  directly visible.
- Pass/fail: no "Unmanaged memory use is high" / "Pausing worker" lines
  for the whole small run, and `atlantis batch gfm cube status` (against
  the scratch db) shows `DONE == total`, `FAILED == 0`.

**2.3 Resume the real 2025 backlog** _(depends on 2.2 passing)_:

- Re-run `atlantis batch gfm cube run` against the CONFIRMED real
  `--archive` and confirmed yearly catalogue (drop `--partition`, or use
  one spanning the whole file — safe either way since `task_id =
f"gfm-{date}-{equi7_tile}"` [`src/atlantis/fetchers/gfm/inventory.py`
  `to_tasks()`] depends only on catalogue rows, not on partition/archive,
  so widening scope just means already-DONE cells are skipped and
  everything else gets (re)tried).
- `--db-path ./gfm_cube_tracker_2025.db` (the downloaded real tracker),
  plus the validated memory/worker flags from 2.2.
- Keep `--gfm-coarsen-factor` / `--gfm-resampling` at defaults (`4` /
  `average`) unless the original run is confirmed to have used something
  else (unconfirmed — see Decisions).
- Run detached (`tmux new -s gfm_cube` / `nohup`) per existing docs
  convention; poll with `atlantis batch gfm cube status --db-path
./gfm_cube_tracker_2025.db --inventory <catalogue>`.

**RAM → settings tiers** (pick a row after checking `free -h`; current
default is `workers-min=2 workers-max=6 memory-limit=4GB`, which the observed
warnings show is too tight for GFM's per-task footprint):

| Host RAM | --workers-min | --workers-max | --memory-limit |
| -------- | ------------- | ------------- | -------------- |
| ~8 GB    | 1             | 1             | 5GB            |
| ~16 GB   | 1             | 2             | 6GB            |
| ~32 GB   | 2             | 3             | 8GB            |
| 64 GB+   | 2             | 4             | 10GB           |

## Relevant files

- `src/atlantis/fetchers/gfm/batch_processor.py` — the code fix: `GDAL_CACHEMAX`
  env default + `_trim_malloc()` helper called after the existing `gc.collect()`
  in `harmonise_gfm_payload`.
- `src/atlantis/fetchers/modis/batch_processor.py` (L55) /
  `src/atlantis/fetchers/viirs/batch_processor.py` (L56) — existing
  `os.environ.setdefault("GDAL_NUM_THREADS", "2")` pattern being mirrored.
- `src/atlantis/archive/cube_batch.py` — `run_cube_batch` / `run_gfm_cube_batch`
  (context only; this is where `LocalCluster(memory_limit=cfg.memory_limit_per_worker, ...)`
  is constructed — no changes planned here per the user's "keep it minimal" choice).
- `src/atlantis/batch/config.py` — `BatchConfig` (workers_min/max,
  memory_limit_per_worker defaults; context only).
- `src/atlantis/batch/tracker.py` — `get_pending`/`mark_done`/`stats`, the
  resume mechanics the retry relies on unchanged (context only).
- `src/atlantis/fetchers/gfm/inventory.py` `to_tasks()` — confirms
  `task_id` is `(date, equi7_tile)`-only, independent of `--partition`/`--archive`.
- `src/atlantis/cli.py` (`gfm_cube_app`, `batch_gfm_cube`, ~L3201-3300) — the
  CLI entry points used for all retry/test commands above (context only).
- `docs/archive/cube-build.md` — existing operational doc/table (§4.1) this
  plan's RAM tiers extend; optional doc update, not required (see Further
  Considerations).

## Verification

1. Run the existing GFM batch tests after the code edit: `tests/fetchers/gfm/test_batch_processor.py`
   and `tests/archive/test_cube_batch.py` (mocked-Dask; per repo memory, test
   against both `uv run pytest` and `pixi run -e batch pytest` since package
   availability differs between envs).
2. Phase 2.2 small test: watch logs / dask dashboard for the whole run —
   zero "Unmanaged memory use is high" / "Pausing worker" lines, tracker ends
   at `DONE == total`, `FAILED == 0`.
3. Before/after the real resume (2.3): `atlantis batch gfm cube status`
   DONE count only increases, FAILED stays at/near 0.
4. During at least the first stretch of the real resume, spot-check worker
   RSS doesn't climb unbounded (dashboard memory plot, or `ps -eo
pid,rss,cmd | grep dask` a couple of times a few minutes apart).

## Decisions

- Code-fix scope limited to `GDAL_CACHEMAX` bound + `malloc_trim` in
  `gfm/batch_processor.py` only — user explicitly declined the optional
  worker-lifetime-recycling safety net for now.
- Original `--archive` root and exact `--partition` are NOT known with
  certainty (user could only point to the tracker's S3 path and the general
  `s3://atlantis/assets/<dataset>/<yearly_collection>` catalogue convention)
  — Phase 2.1 is a mandatory discovery/verification step rather than an
  assumption, since a wrong `--archive` would silently create/diverge a
  second store while the tracker still claims cells are DONE.
- `--gfm-coarsen-factor` / `--gfm-resampling` assumed at defaults (4 /
  average) for the resume since the original values are unconfirmed; flagged
  as a data-consistency caveat (mismatched values wouldn't break the resume
  mechanics but would make old vs. newly-written cells inconsistent).
- Resume reuses the existing SQLite tracker as-is — no schema/code changes
  to `atlantis/batch/tracker.py`.

## Further Considerations

1. If the small test (2.2) still shows warnings after the code fix, the
   next lever (declined for now) is proactive worker recycling — Dask
   `Worker`/`Nanny` `lifetime` support to restart each worker process after a
   time/task budget so any residual native-memory fragmentation can't
   accumulate for the whole run. Would need a small addition to the
   `LocalCluster(...)` call in `run_cube_batch` (`src/atlantis/archive/cube_batch.py`).
2. The same `GDAL_CACHEMAX` default could be added to
   `modis/batch_processor.py` / `viirs/batch_processor.py` for consistency if
   similar warnings ever show up there — out of scope now (GFM-only issue reported).
3. Optional: once the fix is confirmed, add a one-line note to
   `docs/archive/cube-build.md` §4.1's flag table about the `GDAL_CACHEMAX`
   bound and the "must reuse the same `--archive` to resume" caveat — not
   requested, so left out unless the user asks.
