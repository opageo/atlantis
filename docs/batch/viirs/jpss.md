# VIIRS JPSS 2020 Batch Processing

**1-arcmin global flood-fraction COGs from 48,928 NOAA VIIRS granules**

This document describes how Atlantis converts the full **VIIRS JPSS 2020** archive (NOAA-hosted source GeoTIFFs) into a uniform set of **1-arcmin, `uint8`, Cloud-Optimised GeoTIFFs** stored on the ECMWF object store at `s3://atlantis/viirs/jpss/2020/`.

The design is intentionally **dataset-agnostic at the engine layer** — the same `src/atlantis/batch/` machinery will drive MODIS (and any future dataset) by plugging in a different inventory loader and per-task processor.

---

## Goals

Four properties drive every design choice:

| Goal                       | How it's achieved                                                                                                                                                                                                                                                                                |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Compute-efficient**      | Dask `LocalCluster` with 2–6 adaptive worker processes. Per-granule: download to a `tempfile`, read locally (~2.8× faster than `/vsicurl/` for NOAA's one-row-strip TIFFs), classify, harmonise, encode COG in memory, upload. `GDAL_NUM_THREADS=2` per worker for parallel warp + COG encoding. |
| **Live progress tracking** | Dask dashboard (`http://VM:8787`) + a periodic loguru line: `[12400/48000] 26% · 583/hr · ETA ~61h · failures: 23 · retries: 47`.                                                                                                                                                                |
| **Crash-safe resume**      | One SQLite DB per VM records `(task_id, status, output_uri, error, attempts)`. On restart, finished IDs are filtered before futures are submitted — only pending work runs.                                                                                                                      |
| **Disk-space lean**        | Output is `uint8` (0–100 percent, `nodata=255`), not `float32` (~75% smaller). One COG per granule (flood_fraction only), DEFLATE + `predictor=2`, ~0.5–2 MB each. Peak local disk during the run is bounded by `workers_max × ~20 MB` ≈ 120 MB.                                                 |

---

## Tech stack

| Layer         | Choice                                                                              | Reason                                                                                                                                                                                                  |
| ------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Parallelism   | [`dask.distributed.LocalCluster`](https://distributed.dask.org/)                    | Native dashboard, declarative retries (`retries=3`), clean path to multi-node later. Adaptive `workers_min/max` handles per-granule memory variability.                                                 |
| Per-task API  | `client.map()` + `as_completed()`                                                   | Streams results back as they finish so the SQLite tracker is always current — survives multi-day runs and VM crashes.                                                                                   |
| Worker model  | 2–6 **processes**, 1 thread each (Python)                                           | `rasterio` is process-safe but not always thread-safe. GDAL's internal C-thread pool (`GDAL_NUM_THREADS`) is independent of the Python GIL, so we still get warp/encode parallelism inside each worker. |
| Source I/O    | `requests.get(..., stream=True)` → `tempfile.mkstemp()`                             | NOAA source TIFFs are pathological (one-row strips, uncompressed) so `/vsicurl/` issues thousands of small range reads. A single sequential GET is ~2.8× faster. Tempfile is unlinked in a `finally`.   |
| Source format | Plain GeoTIFF, 375 m, ~20 MB each                                                   | Decoded with `classify_viirs_flood_fraction` — a lightweight, picklable module-level sibling of `classify_viirs_pixels` that returns only the flood-fraction array.                                     |
| Reprojection  | `Harmoniser` (existing)                                                             | Same reproject + 1-arcmin-grid logic the rest of Atlantis uses. We don't fork the science.                                                                                                              |
| Output format | `rasterio.MemoryFile(driver="COG", compress="DEFLATE", blocksize=512, predictor=2)` | True COG with `overviews="AUTO"` (GDAL chooses the levels). Output is ~2–3 KB → no point touching disk.                                                                                                 |
| Upload        | `s3fs.S3FileSystem(endpoint_url=...)`                                               | Custom endpoint = ECMWF object store. Credentials come from the `default` boto3 profile written by `atlantis setup`.                                                                                    |
| Resume DB     | **SQLite, one DB per VM**                                                           | No shared state, no NFS coordination. Two-VM split = trivial `UNION ALL` merge afterwards.                                                                                                              |
| Packaging     | `atlantis[batch]` extras                                                            | Keeps the core install lean. Adds `dask[distributed]`, `bokeh`, `rio-cogeo`, and pulls in `[geo]` transitively.                                                                                         |

---

## Per-granule pipeline

Each Dask worker downloads one source granule at a time to a `tempfile`, processes it locally, uploads the resulting COG, and unlinks the tempfile in a `finally` block.

```text
NOAA S3 granule (375 m GeoTIFF, ~20 MB)
  │
  │  requests.get(url, stream=True) → tempfile.mkstemp()
  ▼                                              ← ~1.6 s sequential GET
 /tmp/viirs_src_XXXXXX.tif
  │
  │  rasterio.open(local_path)                  ← ~0.02 s (vs ~4.7 s via /vsicurl/)
  ▼
 raw array
  │
  │  classify_viirs_flood_fraction()            ← keep ONLY flood_fraction
  ▼                                              (discard non-flood derived layers and raw codes)
 flood_fraction (float32, native 375 m)
  │
  │  Harmoniser.harmonise()                     ← reproject + regrid to 1 arcmin
  ▼                                              (GDAL_NUM_THREADS=2)
 flood_fraction (float32, 1 arcmin)
  │
  │  scale [0, 1] → uint8 [0, 100], NaN → 255   ← mirrors write_harmonised_raster
  ▼
 flood_fraction (uint8, 1 arcmin)
  │
  │  rasterio.MemoryFile(driver="COG", ...)     ← DEFLATE, 512×512, overviews=AUTO
  ▼                                              (output ~2–3 KB — stays in memory)
 COG bytes (in-memory)
  │
  │  s3fs.open(dest, "wb").write(bytes)
  ▼
 s3://atlantis/viirs/jpss/2020/{date}/GLB{aoi_id:03d}.tif
  │
  │  finally: src_path.unlink(missing_ok=True)  ← tempfile cleanup
  │  TaskResult → main process
  ▼
 tracker.mark_done(task_id, output_uri)         ← SQLite, one row per granule
```

---

## Module layout

```
src/atlantis/batch/                  # dataset-agnostic engine
├── __init__.py                      # run_batch, BatchConfig, TaskResult
├── orchestrator.py                  # LocalCluster + Client.map + as_completed
├── tracker.py                       # SQLite resume layer (per-VM)
└── config.py                        # BatchConfig dataclass

src/atlantis/fetchers/viirs/         # VIIRS-specific bindings
├── inventory.py                     # load_inventory, slice_partition, to_tasks
└── batch_processor.py               # process_granule(task) -> TaskResult

src/atlantis/cli.py                  # +atlantis batch viirs run subcommand
```

**Separation of concerns:**

- `batch/*` knows nothing about VIIRS, COGs, or S3. It accepts a list of task dicts and a `process_fn(task) -> TaskResult` callable.
- `fetchers/viirs/inventory.py` knows about the catalogue Parquet schema and produces task dicts.
- `fetchers/viirs/batch_processor.py` is the per-task callable that does the actual science.

This makes MODIS a drop-in: add `fetchers/modis/inventory.py` + `fetchers/modis/batch_processor.py`, and the orchestrator, tracker, and CLI scaffolding stay identical.

---

## Inputs & outputs

### Source catalogue

`s3://atlantis/assets/viirs/jpss/2020/catalogue.parquet` (1.4 MB). Schema:

| Column     | Type                  | Description                                                     |
| ---------- | --------------------- | --------------------------------------------------------------- |
| `date`     | `object` (YYYY-MM-DD) | Granule date, 366 unique values (full year incl. leap day)      |
| `aoi_id`   | `int64`               | AOI tile id, 134 unique values in `[1, 136]`                    |
| `s3_key`   | `object`              | NOAA-relative key                                               |
| `geometry` | WKB bytes             | AOI bounding polygon (lat/lon) — not used by the batch pipeline |

48,928 rows, no nulls, no duplicates. Already filtered to JPSS VFM 1-day GLB 2020 — `inventory.py` does not need a `filter(year, product)` helper.

**Derivations** (all done in `to_tasks()`):

- `task_id` = `Path(s3_key).stem` — unique per row, used as SQLite primary key
- `source_uri` = `https://noaa-jpss.s3.amazonaws.com/{s3_key}`
- `dest_key` = `viirs/jpss/2020/{date}/GLB{aoi_id:03d}.tif`

### Output COGs

| Property      | Value                                                                                                            |
| ------------- | ---------------------------------------------------------------------------------------------------------------- |
| S3 location   | `s3://atlantis/viirs/jpss/2020/{date}/GLB{aoi_id:03d}.tif`                                                       |
| Variable      | `flood_fraction` only                                                                                            |
| Dtype / range | `uint8`, 0–100 (percent), `nodata=255`                                                                           |
| Resolution    | 1 arcmin (`HarmoniseConfig.target_resolution_arcmin=1.0`)                                                        |
| Format        | `driver="COG"`, `compress="DEFLATE"`, `blocksize=512`, `predictor=2`, `overviews="AUTO"` (resampling: `average`) |
| Endpoint      | `https://object-store.os-api.cci1.ecmwf.int` (ECMWF object store)                                                |

The `{date}/GLB{aoi_id:03d}.tif` key shape is browsable per-day with `ecaws s3 ls` and uniquely determined by `(date, aoi_id)`, so re-runs are idempotent (same input → same output URI → S3 last-writer-wins).

---

## Running it

### Prerequisites

```bash
uv pip install -e ".[batch,geo]"
atlantis setup                          # writes the `default` boto3 profile pointing at the ECMWF endpoint
```

### Single VM

```bash
atlantis batch viirs run \
  --inventory s3://atlantis/assets/viirs/jpss/2020/catalogue.parquet \
  --output    s3://atlantis/viirs/jpss/2020/ \
  --workers-min 2 --workers-max 6 \
  --memory-limit 6GB \
  --db-path tracker.db
```

### Recommended worker settings

The CLI defaults (`--workers-min 2 --workers-max 6`) are conservative. For most VMs you'll want to **fix** the worker count instead of relying on adaptive scaling — for our short (~2–5 s) granules the queue drains too fast for adaptive to scale up, and the cluster lingers at `workers_min`.

Pick a profile based on what the VM has:

| Profile             | Flags                                                    | Best for                                                                                      | Expected throughput | Full-run time (48,928) |
| ------------------- | -------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ------------------- | ---------------------- |
| **Comfortable**     | `--workers-min 4  --workers-max 4  --memory-limit 4GB`   | 4-core / 16 GB VM, or alongside other jobs                                                    | ~1,500 granules/hr  | ~32 h                  |
| **Recommended** ⭐  | `--workers-min 12 --workers-max 12 --memory-limit 2.5GB` | 8-core / 32 GB VM — over-subscribes cores (downloads are I/O-bound)                           | ~4,500 granules/hr  | ~11 h                  |
| **Push the limits** | `--workers-min 16 --workers-max 16 --memory-limit 1.8GB` | Same 8-core / 32 GB VM with the perf changes (lean classifier + `gc.collect()` between tasks) | ~6,000 granules/hr  | **~8 h**               |

> Each worker uses ~0.5–1.3 GB RSS plus ~20 MB tempfile. `--memory-limit` is a Dask cap (kills the worker if exceeded); leave headroom over the actual RSS so transient spikes don't trigger restarts. Total RAM = `workers × memory_limit` should stay under ~80 % of system RAM.

**Why fixed, not adaptive?** Adaptive is excellent for variable workloads. For a uniform batch like this, fixed workers give cleaner throughput, simpler monitoring, and avoid the "cluster never scaled past minimum" surprise.

#### Healthy CPU pattern

You'll see CPU oscillate between ~50 % and ~99 % across all cores in clear waves. That's the workers cycling in lockstep:

| Phase                             | CPU       | Bottleneck |
| --------------------------------- | --------- | ---------- |
| Download granule from NOAA        | ~50 %     | network    |
| Classify + harmonise + encode COG | ~99 %     | CPU        |
| Upload tiny COG to S3             | brief dip | network    |

Both resources alternate — that's optimal. If CPU stays pinned at 99 % the whole time you're CPU-bound (lower workers), if it stays low you're network-bound (raise workers).

#### Example: full archive on the recommended profile

```bash
uv run atlantis batch viirs run \
  --workers-min 12 --workers-max 12 \
  --memory-limit 2.5GB \
  --db-path tracker_full.db \
  --log-every 200
```

### Monitoring

Three options. Any one is enough; the SQLite tracker is the source of truth.

```bash
# 1. Live progress — refreshes every 2 s, works in any terminal
watch -n 2 "sqlite3 tracker_full.db 'SELECT status, COUNT(*) FROM tasks GROUP BY status'"

# 2. CPU + RAM per worker — press F4 and type 'python3' to filter
htop
```

#### 3. Dask dashboard

The dashboard runs **inside the orchestrator process** on the VM at `localhost:8787`. To view it from your laptop, open an SSH tunnel **on your laptop** (not on the VM) and **leave it running** for the whole batch:

```bash
ssh -L 8787:localhost:8787 -L 8786:localhost:8786 user@vm-host
```

Then point your laptop browser at <http://localhost:8787>. The dashboard shows the task graph, per-worker memory, throughput, and exception tracebacks live.

| Tip                                         | Why                                                                                                                                          |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Forward **both** 8787 and 8786              | 8787 serves the Bokeh page; 8786 is the scheduler the page connects back to for live updates.                                                |
| The page only works while the run is active | The dashboard is a Python thread inside the orchestrator — it disappears the moment `atlantis batch viirs run` exits.                        |
| Re-tunnel after restarting a run            | The port forward survives, but VS Code's "Ports" panel sometimes marks the port broken after the upstream disappeared — re-add it if needed. |
| No dashboard? You're not blocked            | The `watch sqlite3 …` + `htop` combo covers ~90 % of what the dashboard offers.                                                              |

### Two-VM split (deterministic, no coordination)

```bash
# VM1
atlantis batch viirs run --partition 0:24464      --db-path tracker_vm1.db
# VM2
atlantis batch viirs run --partition 24464:48928  --db-path tracker_vm2.db

# After both finish — merge the two trackers locally
sqlite3 merged.db <<'EOF'
  ATTACH 'tracker_vm1.db' AS a;
  ATTACH 'tracker_vm2.db' AS b;
  CREATE TABLE tasks AS
    SELECT * FROM a.tasks UNION ALL SELECT * FROM b.tasks;
EOF
```

`slice_partition` sorts by `(date, aoi_id)` before slicing, so the partitioning is reproducible across machines and re-runs.

### Resume after a crash

Just re-run the **same command** with the **same `--db-path`**. The orchestrator queries `get_pending(all_task_ids)` against the SQLite DB and submits futures only for tasks that aren't yet `DONE`. `FAILED` tasks are also re-queued, so transient errors get another chance.

---

## Operational profile

Measured on a single 8-core / 32 GB Linux VM with 4 active workers:

| Metric                              | Value                                                                          |
| ----------------------------------- | ------------------------------------------------------------------------------ |
| Per-granule time                    | ~2–5 s end-to-end (~1.6 s download + ~0.5–2 s classify+harmonise+upload)       |
| Smoke-test throughput (10 granules) | **~13 s wall time** (~46 granules/min sustained)                               |
| Extrapolated full run               | **~17 h single-VM**, ~8–9 h split across two VMs                               |
| Local disk usage                    | Bounded by `tempfile` lifetime: `workers_max × ~20 MB` ≈ **120 MB peak**       |
| Output size per COG                 | ~0.5–2 MB                                                                      |
| Total output size                   | ~24–98 GB for 48,928 granules                                                  |
| Peak RAM                            | adaptive, capped at `workers_max × memory_limit_per_worker` (≈30 GB at 6×5 GB) |
| Dashboard                           | `http://VM_IP:8787` — task graph, worker memory, throughput, exceptions        |

For comparison, the original **streaming `/vsicurl/`** prototype managed ~1,200 granules/hr (full run ~40 h single-VM) because NOAA's source TIFFs are one-row strips that trigger thousands of small range reads. **Local staging via `tempfile` is ~2.3× faster end-to-end.**

---

## Why these choices

### Dask `LocalCluster` (not Celery, not Ray, not raw multiprocessing)

- **Dashboard for free**: live task graph, worker memory, throughput, exception inspection at `:8787`. No extra ops.
- **Declarative retries**: `client.map(fn, items, retries=3)` re-submits failed tasks without any hand-rolled backoff loop. Combined with `time.sleep(2**attempt)` inside `process_granule`, this absorbs NOAA S3 503 throttling.
- **Adaptive scaling** (`cluster.adapt(2, 6)`): the cluster grows and shrinks based on workload, handling per-granule memory variability gracefully.
- **Future-proof**: the exact same code that runs on a `LocalCluster` works on a multi-node `SSHCluster` or a Kubernetes cluster. We can migrate later without rewriting `process_granule`.

### `client.map()` + `as_completed()` (not `compute()`, not `gather()`)

We need results to flow back **as they complete**, not in a batch at the end. This is what makes the SQLite tracker always current — pull the plug at any moment and we never lose more than the few in-flight granules. `as_completed()` gives us that streaming behaviour without us having to hand-write any callbacks.

### SQLite resume (not Redis, not a flat file)

- **One file**, no daemon. Survives reboots, no port to manage, no network dependency.
- **One DB per VM**: zero coordination between VMs means we can split a workload by partition and never touch shared state. `UNION ALL` merges the trackers afterwards if we want a unified view.
- **Upsert semantics** (`INSERT … ON CONFLICT(task_id) DO UPDATE`): retrying a previously failed task just overwrites its row. Idempotent.
- A flat file could work but would either need locking or sequential writes — SQLite handles both for free.

### Local staging via `tempfile` (not streaming `/vsicurl/`, not a persistent cache dir)

Measured: **~2.8× faster** per granule on real NOAA sources. The win comes from issuing one sequential GET instead of thousands of small range reads against pathological one-row-strip TIFFs.

`tempfile.mkstemp()` was chosen over `tempfile.NamedTemporaryFile` because we want to control the lifetime explicitly via `finally`-block `unlink()`, not via a context manager's auto-cleanup (which is awkward to combine with the rest of the pipeline). The path comes from `mkstemp` directly with a clear prefix (`viirs_src_`) so any leaked files are easy to spot in `/tmp`.

A persistent cache directory was considered and rejected: we'd need an LRU eviction policy, disk-pressure handling, and a way to invalidate cached files when the source changes. None of that is worth it when the input is read exactly once per granule.

### True COG output (not plain GeoTIFF)

The existing `write_harmonised_raster()` in `harmoniser/__init__.py` emits a plain LZW-compressed GeoTIFF — perfect for per-event Atlantis runs (one or two files per event). For a 48,928-file global archive, COG matters: HTTP range reads from QGIS, STAC, COG-readers all need internal tiling + overviews. We use `driver="COG"` directly rather than calling `write_harmonised_raster` so the science stays consistent but the on-disk layout is web-optimised.

The COG is built in a `MemoryFile` because each output is ~2–3 KB. Streaming to a tempfile would add disk I/O for no benefit.

### `uint8` 0–100 (not `float32`, not `uint16`)

Source pixel codes 101–200 encode flood fraction as `(code − 100) %`, so percent is the natural unit. `uint8` with `nodata=255` is consistent with `write_harmonised_raster`'s existing convention, and it's ~75% smaller than `float32`. Combined with DEFLATE+`predictor=2`, typical compressed output is **~2–3 KB per granule** — small enough that overhead from S3 metadata operations dominates upload time, not byte count.

---

## Replicating for another dataset (e.g. MODIS)

1. **Build a catalogue Parquet** with at minimum `(task_id, source_uri, dest_key)` per output. Upload it to `s3://atlantis/assets/<dataset>/<year>/catalogue.parquet`.
2. **Write `src/atlantis/fetchers/<dataset>/inventory.py`** with:
   - `load_inventory(uri)`
   - `slice_partition(df, partition)`
   - `to_tasks(df, output_prefix)` returning a list of task dicts with the keys above (plus whatever extra fields your processor needs).
3. **Write `src/atlantis/fetchers/<dataset>/batch_processor.py`** with a top-level, picklable `process_granule(task) -> TaskResult` function. Mirror the structure of `viirs/batch_processor.py`: download → process → write COG to `MemoryFile` → upload via `s3fs` → `finally: src_path.unlink(missing_ok=True)`.
4. **Add a CLI command** in `cli.py`: copy the `atlantis batch viirs run` pattern, swap the imports.
5. **Run.** The orchestrator, tracker, dashboard, retry, and resume logic stays identical.

---

## Known constraints & gotchas

- **`s3fs` must be instantiated inside `process_granule`** — never at module level. Dask workers are fresh processes, so each one needs to open its own connection pool. `_s3fs_filesystem()` is a regular function (not cached); if profiling shows the per-call overhead is significant we can add `functools.cache`.
- **`GDAL_NUM_THREADS=2`** is `setdefault` so users can override via env. Don't bump above the number of physical cores per worker (1 thread × 8 cores ÷ 6 workers ≈ 1.3 cores each → 2 is fine, 4 would be too many at full adaptive scale).
- **Loguru in workers** — Dask workers are separate processes, so loguru config doesn't propagate. The orchestrator registers `_LoguruWorkerPlugin` to call `logger.add(sys.stderr, enqueue=True)` per worker. Without `enqueue=True`, multiple workers writing simultaneously would interleave their output.
- **NOAA throttling** — NOAA can return HTTP 503 under load. We rely on Dask's `retries=3` plus an exponential backoff inside `process_granule` (currently implicit via `requests`' default retry behaviour; an explicit `time.sleep(2**attempt)` can be added if 503s become a regular issue).
- **Output key collision across VMs**: if two partitions accidentally overlap, S3 is last-writer-wins. Prefer non-overlapping `--partition` ranges and verify with `ecaws s3 ls --recursive s3://atlantis/viirs/jpss/2020/ | wc -l` after the run completes — should equal `48928` minus any FAILED rows in the merged tracker.
