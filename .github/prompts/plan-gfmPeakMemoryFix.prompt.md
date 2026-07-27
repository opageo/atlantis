# Plan: Fix GFM per-cell peak-memory OOM (supersedes Phase 2 of plan-gfmDaskMemoryFix.prompt.md)

**Tracking:** GitHub issue #96 (already open — do not open a new one).

## TL;DR

`plan-gfmDaskMemoryFix.prompt.md`'s Phase 1 code fix (`GDAL_CACHEMAX=256` +
`malloc_trim(0)` in `harmonise_gfm_payload`,
[batch_processor.py](../../src/atlantis/fetchers/gfm/batch_processor.py)) is
merged, safe, and verified against the full suite — but it does **not** fix
the OOM. Two disposable local test runs proved the plan's root-cause
hypothesis (cross-task GDAL cache / native-memory accumulation) wrong: **a
single GFM `(date, equi7_tile)` cell has a ~15 GiB _peak transient_ memory
footprint on its own**, before a worker has processed more than one task. A
per-task peak that large can't be fixed by bounding a cache or trimming heap
fragmentation between tasks — the fix has to reduce what one task allocates
at once. The real production backlog (`s3://atlantis/db/archive_tracker_gfm_2025.db`)
is confirmed still at 0/205635 DONE with zero Zarr chunks written, so this is
a fresh start, not a resume.

This plan does two things differently from the superseded one:

1. **Measures before implementing.** The last attempt shipped a fix for an
   unverified hypothesis. This time, profiling which stage of the pipeline
   actually drives the ~15 GiB is a mandatory step _before_ writing the real
   fix (Phase C.1 below).
2. **Separates "unblock now" from "fix it properly"** so the team can choose:
   run a slow-but-safe single-worker stopgap immediately (Phase B, optional),
   while the real per-cell peak-memory fix (Phase C, recommended) is
   engineered and validated in parallel.

**My recommendation:** prioritize Phase C over shipping Phase B as a
permanent state. At single-worker throughput, 205,635 cells will take a long
time (measure the real rate from whichever run you do first — not
guessing at a number here), and fixing the actual root cause also very likely
restores real multi-worker parallelism (peak drops → more workers fit per
host), which compounds the throughput win instead of just tolerating it.

## Confirmed coordinates (already discovered — do not re-discover)

Per `/memories/repo/gfm-investigation.md` ("Dask worker-memory investigation"):

- Catalogue: `s3://atlantis/assets/gfm/gfm_archive_catalog_2025.parquet` (205,635 rows/tasks)
- Tracker: `s3://atlantis/db/archive_tracker_gfm_2025.db` (confirmed EMPTY — 0 DONE / 0 FAILED)
- Archive: `s3://atlantis/zarr/2025` (confirmed `datacube.zarr/gfm/water_fraction/` has zero data chunks — only `zarr.json`)

## Steps

### Phase A — Correct the operational guidance _(no code, do first, independent)_

1. [docs/archive/cube-build.md](../../docs/archive/cube-build.md) line ~292:
   the `--memory-limit` table row currently says GFM "needs VIIRS-level
   headroom" (i.e. ~4GB) — this is now **proven wrong** by the ~15 GiB
   measured peak and is exactly the guidance that likely caused the original
   production run to die before completing a single cell. Replace it with the
   measured reality and a pointer to issue #96 for current status, so nobody
   trusts the old number again while Phase C is in flight.
2. No repo-memory changes needed — `/memories/repo/gfm-investigation.md`
   already has the full investigation log; keep appending to it rather than
   creating a new memory file.

### Phase B — Optional immediate stopgap, no code change _(independent of Phase C; skip if the team prefers to wait for the real fix)_

1. Only do this if partial forward progress on the real backlog is wanted
   _now_, in parallel with Phase C's development. It is a throughput
   sacrifice, not a fix.
2. Pick **one host**, check `free -h`, and set:
   - `--memory-limit` ≥ 20GB, prefer 24GB if the host can spare it — margin
     over the measured ~15.2 GiB peak. Test2 (20GB) still paused ~once per
     cell (recovered gracefully); that sample was only 2 cells, so treat 20GB
     as workable-but-tight, not a fully-validated floor — cells with more
     than the 1-2 STAC items seen in testing (e.g. more overlapping
     ascending/descending passes) could peak higher.
   - `--workers-min 1 --workers-max 1` initially. Only raise `--workers-max`
     after confirming stability, and only if `workers-max × memory-limit`
     comfortably fits the host's free RAM (`LocalCluster` does not stop you
     from over-committing — see the diagnosis note below).
3. Run against the **confirmed real coordinates** above — this is genuinely
   starting from scratch, not resuming, so there's no risk of double-writing
   over prior progress:
   ```bash
   PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch gfm cube run \
     --inventory s3://atlantis/assets/gfm/gfm_archive_catalog_2025.parquet \
     --archive s3://atlantis/zarr/2025 \
     --db-path ./gfm_cube_tracker_2025.db \
     --workers-min 1 --workers-max 1 --memory-limit 24GB
   ```
   Download the tracker first (`aws s3 cp s3://atlantis/db/archive_tracker_gfm_2025.db ./gfm_cube_tracker_2025.db --endpoint-url https://object-store.os-api.cci1.ecmwf.int`). Run detached (`tmux`/`nohup`).
4. Monitor with `atlantis batch gfm cube status --db-path ./gfm_cube_tracker_2025.db --inventory <catalogue>` and the Dask dashboard (port 8789). DONE should climb, FAILED should stay ~0. If FAILED starts climbing, stop and raise `--memory-limit` further before retrying.
5. **Out of scope:** multi-host / multiple concurrent invocations against the
   same `--db-path` for more throughput. A single `run_cube_batch` call's
   SQLite writes all come from one coordinator process regardless of worker
   count (safe), but two _separate_ `atlantis batch gfm cube run` processes
   writing the same `--db-path` concurrently is not a supported pattern today
   ([`atlantis/batch/tracker.py`](../../src/atlantis/batch/tracker.py) has no
   multi-writer locking) — the documented "safe to run concurrently" pattern
   in cube-build.md is about different _sources_ (viirs/modis/gfm) each with
   their own `--db-path`, not two GFM runs sharing one. If more throughput is
   needed before Phase C lands, use `--workers-max` on a single bigger host,
   not multiple hosts.

**Diagnosis note (why Test1 failed so fast):** `LocalCluster` doesn't refuse
to start workers whose combined declared `memory_limit` exceeds host RAM —
Test1 (2-3 workers × 8GB = 16-24GB declared, on a 32GB host) started fine but
every worker was killed/restarted within ~8s. That matches Dask's own
worker/nanny self-management (pause around ~0.8× `memory_limit`, kill+restart
around ~0.95×) kicking in on the _first_ task, because a single task's ~15GB
peak already exceeds an 8GB declared limit — not a host-level OOM. This means
`--memory-limit` must individually exceed the true per-task peak; adding more
workers doesn't help a too-low per-worker limit.

### Phase C — Real fix: reduce per-cell peak memory _(recommended primary path, independent of Phase B)_

#### C.1 Instrument and attribute the peak _(mandatory measurement gate before writing any fix — this is the step skipped last time)_

Profile peak memory (native + Python, not just Python-tracked objects — a
plain `tracemalloc` run would miss GDAL/numpy native buffers) attributable to
each stage inside
[`GfmRasterProcessor._process_items_classified`](../../src/atlantis/fetchers/gfm/processor.py#L398)
(the `classify=True` path `harmonise_gfm_payload` actually uses):
a. [`_load_item()`](../../src/atlantis/fetchers/gfm/processor.py#L323) — the
eager `odc.stac.load(...).load()` call fetching all 6
[`GFM_BANDS`](../../src/atlantis/fetchers/gfm/layers.py#L44) at native
(~20m) resolution over the whole EQUI7 tile bbox (up to 15000×15000 px).
b. [`_build_native_masks()`](../../src/atlantis/fetchers/gfm/processor.py#L688) —
native-resolution float32 binarization of 3 bands before the
coarsen-mean.
c. The `code_bands` / `likelihood_band` construction inline in
`_process_items_classified` (~L470-490) — native-res copies, including
the `.astype("float32")` cast on the full-resolution `ensemble_likelihood`
band.
d. [`_reproject_to_canonical_grid()`](../../src/atlantis/fetchers/gfm/processor.py#L718) /
[`_reproject_codes_to_canonical_grid()`](../../src/atlantis/fetchers/gfm/processor.py#L743) /
[`_reproject_likelihood_to_canonical_grid()`](../../src/atlantis/fetchers/gfm/processor.py#L765) —
rioxarray/rasterio/GDAL warp calls; check whether GDAL upcasts
internally (e.g. float64 working buffers for `Resampling.average`)
rather than assuming it's cheap just because the destination array is
small.

Use whichever measurement mechanism is available and reliable in the
executing environment — a Memray-based region profile (VS Code's
Pylance profiling tooling supports marking a region between two source
locations, if available) is ideal for per-stage attribution; failing that, a
minimal stdlib harness (`resource.getrusage(resource.RUSAGE_SELF).ru_maxrss`
sampled between stages, Linux-only, zero new dependencies) is an acceptable
fallback. Run it against **one real cell already known to be expensive**
(reuse a `task_id`/`item_hrefs`/`bbox` from the Test2 disposable run) so the
numbers reflect the actual workload, not a synthetic guess.

Output: a ranked breakdown of which stage(s) actually drive the ~15 GiB peak.
This determines which C.2 option to implement — do not skip straight to
"the obvious fix" the way the superseded plan did.

#### C.2 Implement the targeted fix, guided by C.1's findings

- **Leading hypothesis (lower risk, try first unless C.1 points elsewhere):**
  split the single 6-band `odc.stac.load` call in `_load_item` into two
  smaller loads — the 3 "mask" bands (`ensemble_flood_extent`,
  `ensemble_water_extent`, `reference_water_mask`) processed and
  reprojected-away first, then the "code/likelihood" bands (`exclusion_mask`,
  `advisory_flags`, `ensemble_likelihood`, plus `reference_water_mask` reused)
  — so at most ~3 native-resolution band buffers are ever resident at once
  instead of 6 bands plus several native-res derived copies coexisting, as
  today. Also defer the `ensemble_likelihood` float32 cast until _after_ its
  average-reprojection to the canonical grid, so the cast applies to a small
  array, not the full native-resolution one.
- **Fallback (bigger win, bigger risk — only if C.1 shows the load/derive
  steps aren't the dominant cost, e.g. GDAL warp internals are):** keep the
  native-resolution `odc.stac.load` dask-backed (real `chunks=`, not
  `chunks={}`) and defer `.load()`/`.compute()` until _after_ the
  coarsen-mean step, so the binarize+coarsen pipeline runs block-by-block
  instead of materializing the full native array at once. Known risk: this
  runs a dask array inside a task already scheduled by the outer Dask
  distributed cluster (nested-scheduler gotcha) — would need
  `dask.config.set(scheduler="synchronous")` (or careful `"threads"` use)
  scoped to this call to avoid spawning a second distributed client from
  inside a worker.
- Implement one option, then **re-run C.1's harness** to confirm the peak
  actually dropped by a meaningful margin before considering this done.
  Target: low enough that GFM can go back to the same
  `--memory-limit`/worker-count conventions as VIIRS/MODIS (i.e. real
  multi-worker parallelism becomes viable again), not just "under 15 GiB."

#### C.3 Regression-proof it

- The existing [`tests/fetchers/gfm/test_batch_processor.py`](../../tests/fetchers/gfm/test_batch_processor.py)
  mocks `GfmRasterProcessor.process_items` entirely, so it never exercises
  the actual hotspot. Add a small test (e.g.
  `tests/fetchers/gfm/test_processor_memory.py`) that runs the real
  `_process_items_classified` path against a small synthetic multi-band
  raster (fake `pystac.Item` + a monkeypatched `odc.stac.load` returning an
  in-memory Dataset scaled down proportionally, e.g. 512×512 instead of
  15000×15000) and asserts peak RSS during the call stays under a
  proportionally-scaled bound. This won't catch every regression at full
  tile scale, but guards against a future change reintroducing "hold several
  full-resolution band copies simultaneously."
- Run the full suite in both environments per repo convention (package
  availability differs between them — see `/memories/repo/testing.md`):
  `uv run pytest` and `pixi run -e batch pytest`.

### Phase D — Validate small, then run the real 205,635-cell backlog _(depends on Phase C)_

1. Repeat the superseded plan's small-disposable-test pattern (scratch
   `--archive`/`--db-path`, small `--partition` slice), but now against the
   Phase C fix. Confirm: zero "Pausing worker"/"Unmanaged memory" lines for
   the whole run, and a materially lower measured peak than the pre-fix ~15
   GiB (via C.1's harness or the Dask dashboard).
2. Re-tune `--memory-limit`/`--workers-max` now that peak is (hopefully)
   VIIRS/MODIS-class — likely back to something close to the existing
   `workers_min=2 workers_max=6 memory_limit=4GB`-class defaults, restoring
   real multi-worker parallelism instead of the Phase B single-worker
   stopgap.
3. Run the real backlog against the confirmed coordinates (no re-discovery
   needed):
   ```bash
   PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch gfm cube run \
     --inventory s3://atlantis/assets/gfm/gfm_archive_catalog_2025.parquet \
     --archive s3://atlantis/zarr/2025 \
     --db-path ./gfm_cube_tracker_2025.db \
     --log-every 50
   ```
   Re-download the tracker first in case Phase B's stopgap already advanced
   it — if so, this is now a genuine resume; if not, it's still effectively
   starting fresh. Run detached; poll with `atlantis batch gfm cube status`.

## Relevant files

- [`src/atlantis/fetchers/gfm/processor.py`](../../src/atlantis/fetchers/gfm/processor.py) —
  `GfmRasterProcessor` (`class` at L186); `_load_item` (L323), `process_items`
  (L353), `_process_items_classified` (L398, the hotspot), `_process_items_native`
  (L573), `_build_native_masks` (L688), the three `_reproject_*_to_canonical_grid`
  methods (L718/L743/L765), `_classify` (L808) — Phase C's primary target.
- [`src/atlantis/fetchers/gfm/layers.py`](../../src/atlantis/fetchers/gfm/layers.py) L44 —
  `GFM_BANDS` (the 6 native bands to split into groups for C.2's leading option).
- [`src/atlantis/fetchers/gfm/dataset.py`](../../src/atlantis/fetchers/gfm/dataset.py) —
  `processed_tile_to_dataset` — context only; runs on already-canonical-grid-sized
  arrays so it's an unlikely hotspot, but keep it in C.1's profiling scope to confirm.
- [`src/atlantis/fetchers/gfm/batch_processor.py`](../../src/atlantis/fetchers/gfm/batch_processor.py) —
  `harmonise_gfm_payload` — Phase 1's `GDAL_CACHEMAX`/`_trim_malloc` fix stays
  as-is (harmless, kept); no further change planned here.
- [`src/atlantis/archive/cube_batch.py`](../../src/atlantis/archive/cube_batch.py) —
  `run_cube_batch` — confirms `LocalCluster(..., threads_per_worker=1, ...)`,
  i.e. one task per worker process at a time; no code change planned.
- [`src/atlantis/batch/config.py`](../../src/atlantis/batch/config.py) —
  `BatchConfig` (`workers_min`/`workers_max`/`memory_limit_per_worker` defaults) — context only.
- [`src/atlantis/batch/tracker.py`](../../src/atlantis/batch/tracker.py) —
  no multi-writer locking; grounds Phase B's "single host only" constraint.
- [`src/atlantis/cli.py`](../../src/atlantis/cli.py#L3207) — `batch_gfm_cube`
  (`gfm_cube_app`, L3201-3246) — CLI entry point for all Phase B/D runs.
- [`docs/archive/cube-build.md`](../../docs/archive/cube-build.md) line ~292 —
  the RAM/flag table Phase A corrects.
- [`tests/fetchers/gfm/test_batch_processor.py`](../../tests/fetchers/gfm/test_batch_processor.py) —
  existing tests mock `process_items` entirely (the testing gap Phase C.3 fills).
- [`tests/archive/test_cube_batch.py`](../../tests/archive/test_cube_batch.py) — mocked-Dask cube batch tests, context only.
- `/memories/repo/gfm-investigation.md` — the running investigation log; keep appending here, not a new file.

## Verification

1. Phase A: docs change only — proofread the corrected table row and issue #96 link.
2. Phase B (if used): tracker DONE count climbs, FAILED stays ~0, no repeated worker kills in the first several cells.
3. Phase C.1: profiling produces a clear, numeric per-stage breakdown (not a guess) of the ~15 GiB peak.
4. Phase C.2/C.3: full suite green in both `uv run pytest` and `pixi run -e batch pytest`; new memory-regression test passes; re-measured peak is materially lower than ~15 GiB (quantify once measured, e.g. in the PR description).
5. Phase D: small validation run has 0 failures / 0 memory warnings; real backlog resume shows DONE increasing, FAILED at/near 0, and worker RSS stable over time on spot-check (dashboard memory plot or periodic `ps -eo pid,rss,cmd | grep dask`).

## Decisions

- Recommending Phase C (real fix) as primary, not Phase B (stopgap) as a
  permanent state — flagging this for the user to confirm or override, not
  assuming silently.
- Phase B is explicitly optional and independent of Phase C — do it only if
  partial progress now is wanted.
- Multi-host / multi-tracker-db parallelism for Phase B is explicitly out of
  scope — `atlantis/batch/tracker.py` has no multi-writer locking today, and
  adding it isn't part of this plan.
- No new GitHub issue — user confirmed issue #96 already tracks this. No
  issue/PR comments will be posted without being asked first.
- C.2's fallback (dask-lazy windowed reprojection) is deliberately gated
  behind C.1's measurement rather than implemented up front — avoids
  building a bigger, riskier rewrite than the data says is necessary, and
  avoids repeating the superseded plan's mistake of committing to an
  unverified hypothesis.
- The Phase 1 `GDAL_CACHEMAX`/`malloc_trim` fix (already merged) stays as-is
  regardless of which C.2 option is chosen — harmless, and may still help
  marginally with cross-task fragmentation once per-task peaks are no longer
  the dominant problem.

## Further Considerations

1. Once Phase C lands, consider a defensive review of whether MODIS/VIIRS
   batch processors have any analogous "hold several full-resolution copies
   at once" pattern — out of scope now, since only GFM has been shown to
   have this problem.
2. If Phase C gets GFM's peak close to VIIRS/MODIS levels, consider
   simplifying `docs/archive/cube-build.md`'s flag table back to one shared
   `--memory-limit` row across all three sources (dropping the GFM special
   case) once proven stable on a real multi-week run.
3. If Phase B's stopgap is used and later Phase C's fix changes output
   numerically in any way (it shouldn't, since it only changes _how_ the same
   pixels are computed, not the math) — re-verify a handful of Phase-B-written
   cells aren't inconsistent with cells written after Phase C. Expected to be
   a non-issue since C.2's options only change loading/memory strategy, not
   the accumulation math, but worth a spot-check given how wrong the last
   assumption turned out to be.
