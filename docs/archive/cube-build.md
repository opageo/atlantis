# Building the Zarr Datacube — Operational Guide

> Step-by-step instructions for building the consolidated, multi-source Zarr v3
> datacube from a per-source granule/tile catalogue using the resume-safe,
> streaming batch pipeline. VIIRS and MODIS both plug into the same engine and
> can share one archive.

**Source of truth**

| Concern                                 | Module                                                                                                   |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Cube batch engine                       | [`src/atlantis/archive/cube_batch.py`](../../src/atlantis/archive/cube_batch.py)                         |
| Granule processor (VIIRS)               | [`src/atlantis/fetchers/viirs/batch_processor.py`](../../src/atlantis/fetchers/viirs/batch_processor.py) |
| Tile processor (MODIS)                  | [`src/atlantis/fetchers/modis/batch_processor.py`](../../src/atlantis/fetchers/modis/batch_processor.py) |
| Inventory loader + tasks                | [`.../viirs/inventory.py`](../../src/atlantis/fetchers/viirs/inventory.py) / [`.../modis/inventory.py`](../../src/atlantis/fetchers/modis/inventory.py) |
| Catalogue builder                       | [`.../viirs/catalog.py`](../../src/atlantis/fetchers/viirs/catalog.py) / [`.../modis/catalog.py`](../../src/atlantis/fetchers/modis/catalog.py) |
| Shared catalogue core                   | [`src/atlantis/batch/catalog.py`](../../src/atlantis/batch/catalog.py) (load/slice/write/date-range, reused by every source) |
| CLI (`batch viirs …` / `batch modis …`)  | [`src/atlantis/cli.py`](../../src/atlantis/cli.py#L2448)                                                 |
| Underlying store layout                 | [`zarr-spec.md`](./zarr-spec.md)                                                                         |

> After building the cube, use the [STAC + Visualization guide](./stac-and-viz.md) to
> catalogue and explore it interactively.

---

## 1. Overview

The batch pipeline converts a per-source catalogue (VIIRS granules or MODIS
tiles) into a **single Zarr v3 datacube** (`datacube.zarr`) co-registered on
the canonical global 1-arcmin grid. Every source writes into its own group
(`viirs`, `modis`, ...) inside the same store, so one archive can hold as many
sources as you build into it. The pipeline is:

| Property             | How it works                                                                                                                                 |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| **Parallel**         | Dask `LocalCluster` (2–6 adaptive workers). Each worker downloads, classifies, and harmonises one granule/tile at a time.                    |
| **Resume-safe**      | SQLite tracker records every `(task_id, status, output_uri)`. Re-running skips already-`DONE` tasks.                                         |
| **Streaming**        | `as_completed()` feeds results into a single coordinator that writes to Zarr — no giant in-RAM accumulation.                                 |
| **Crash-proof**      | Run in `tmux` / `nohup`. Kill at any time; re-run to resume from the tracker.                                                                |
| **Dataset-agnostic** | Same engine (`run_cube_batch`) drives VIIRS (`run_viirs_cube_batch`) and MODIS (`run_modis_cube_batch`) by plugging in a different inventory loader and per-task processor — a third source only needs its own thin wrapper. |

---

## 2. Prerequisites

### 2.1 AWS / ECMWF object store credentials

The default catalogues live on `s3://atlantis/` (ECMWF object store). Run once:

```bash
pixi run setup      # or `atlantis setup`
```

This configures the `default` AWS profile with the ECMWF endpoint. Verify:

```bash
aws s3 ls s3://atlantis/ --endpoint-url https://object-store.os-api.cci1.ecmwf.int
```

### 2.2 Pixi environments

The `batch` environment provides Dask, distributed, and all geo dependencies:

```bash
pixi install -e batch
```

### 2.3 MODIS Earthdata token (MODIS only)

Building or refreshing the **MODIS** catalogue queries the NASA LAADS DAAC,
which requires an Earthdata Login bearer token:

```bash
export EARTHDATA_TOKEN="YOUR_TOKEN_HERE"   # or `atlantis setup` to persist it in .env
```

**VIIRS needs no token** — the NOAA JPSS S3 bucket is public/anonymous.

---

## 3. Building or refreshing a catalogue

Each source's catalogue is a Parquet inventory of everything available to
ingest — VIIRS granules or MODIS tiles — built once and re-used (and
periodically extended) across cube builds. Both builders share the same
underlying mechanics ([`atlantis/batch/catalog.py`](../../src/atlantis/batch/catalog.py)):
load/slice/write Parquet, walk an inclusive date range, retry a flaky listing
call. They differ only in schema and remote source:

| | VIIRS (`batch viirs catalog`) | MODIS (`batch modis catalog`) |
| --- | --- | --- |
| Remote source          | NOAA JPSS public S3 bucket (anonymous)         | NASA LAADS DAAC (authenticated)               |
| Auth                   | None                                            | `EARTHDATA_TOKEN` (§2.3)                       |
| Output schema          | `date, aoi_id, s3_key, geometry` (GeoParquet)   | `date, h, v, task_id, source_uri` (Parquet)    |
| Default `--output`     | `viirs_archive_catalog.parquet` (local)         | `modis_archive_catalog.parquet` (local)        |
| Canonical archive path | `s3://atlantis/assets/viirs/viirs_archive_catalog.parquet` | `s3://atlantis/assets/modis/modis_archive_catalog.parquet` |

```bash
# VIIRS — no credentials needed
PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch viirs catalog \
  --start 2025-01-01 --end 2025-12-31 \
  --output s3://atlantis/assets/viirs/viirs_archive_catalog.parquet

# MODIS — requires EARTHDATA_TOKEN
PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch modis catalog \
  --start 2025-01-01 --end 2025-12-31 \
  --output s3://atlantis/assets/modis/modis_archive_catalog.parquet
```

> **Unlike the cube build, the catalogue builder is a plain sequential,
> network-bound loop** — one HTTP request per calendar day, no Dask workers,
> and **no SQLite resume tracker**. If it's interrupted, that run's progress
> is gone and you restart from `--start`. Building MODIS's full history
> (2003–2026, ~8,600 days) can take **hours**; run it detached
> (`tmux`/`nohup`) exactly like the cube build:
>
> ```bash
> tmux new -s modis_catalog
> PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch modis catalog \
>   --start 2003-01-01 --end 2026-07-15 \
>   --output s3://atlantis/assets/modis/modis_archive_catalog.parquet
> ```
>
> Progress prints automatically — a line like `MODIS catalog: 2400/8597
> (27.9%)` every ~30 processed dates — with **no `--verbose` flag needed**
> (it's routed through the CLI's console output, not `loguru`, which the CLI
> disables by default). To confirm a detached run is still alive and
> actually making requests rather than stuck: `pgrep -af "batch modis
> catalog"` and `ss -tnp | grep <pid>` (look for an `ESTABLISHED` connection
> to the source host).

Both default `--output` to a **bare local filename**, not the canonical S3
path — pass `-o s3://atlantis/assets/<source>/...` explicitly once you are
ready to publish the refreshed catalogue.

### 3.1 Extending an existing catalogue

Neither builder merges with what is already on S3 — each invocation produces
a fresh catalogue for the `--start`/`--end` range you give it, and rewrites
whatever is at `--output`. To add a new date range to an existing catalogue
without re-scanning everything already catalogued, build only the new range
locally and concatenate:

```python
import pandas as pd
from atlantis.fetchers.viirs.inventory import load_inventory  # or fetchers.modis.inventory

old = load_inventory("s3://atlantis/assets/viirs/viirs_archive_catalog.parquet")
new = pd.read_parquet("viirs_archive_catalog.parquet")   # just-built range
combined = pd.concat([old, new], ignore_index=True).drop_duplicates(subset=["date", "aoi_id"])
combined.to_parquet("viirs_archive_catalog.parquet", index=False)
# then upload the merged file to s3://atlantis/assets/viirs/viirs_archive_catalog.parquet
```

(Use `subset=["date", "h", "v"]` for MODIS.)

---

## 4. Full workflow — cube, catalogue, visualise

The three-step end-to-end pipeline, per source:

### 4.1 Build the cube

```bash
# VIIRS
PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch viirs cube run \
  --partition 0:1000 \
  --archive s3://atlantis/zarr/my_cube \
  --log-every 50

# MODIS — same --archive, its own tracker, its own row partition
PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch modis cube run \
  --partition 0:1000 \
  --archive s3://atlantis/zarr/my_cube \
  --db-path modis_cube_tracker.db \
  --log-every 50
```

| Flag                | VIIRS default                        | MODIS default                       | Purpose                                |
| ------------------- | ------------------------------------- | ------------------------------------ | --------------------------------------- |
| `--partition`       | full catalogue                        | full catalogue                       | Row slice `start:stop` (e.g. `0:1000`) |
| `--archive` / `-a`  | `s3://atlantis/zarr/viirs_2020_cube`  | `s3://atlantis/zarr/modis_cube`      | Cube root — a local dir or `s3://` URI |
| `--log-every`       | `100`                                  | `50`                                  | Progress line every N completions      |
| `--workers-min/max` | `2` / `6`                              | `2` / `6`                             | Dask worker count (adaptive)           |
| `--memory-limit`    | `4GB`                                  | `2.5GB`                               | Memory cap per worker — MODIS tiles are physically smaller (250 m, fixed 4800×4800) than VIIRS granules (375 m), so they need less headroom |
| `--dashboard-port`  | `8787`                                 | `8788`                                | Distinct ports so both dashboards can run at once |
| `--db-path`         | `cube_tracker.db`                      | `cube_tracker.db`                     | SQLite resume database — **use a different path per source** when writing into the same archive concurrently |
| `--retries`         | `3`                                    | `3`                                   | Retries per granule/tile               |
| `--composite`       | n/a                                    | `None` (→ `F2`)                      | MODIS-only: MCDWD composite to extract (`F1`/`F1C`/`F2`/`F3`) |

The `--archive` value is the **parent** of the Zarr store — the engine creates
`datacube.zarr` underneath it.

> Always run detached. An SSH disconnect kills the coordinator:
>
> ```bash
> tmux new -s cube
> PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch viirs cube run \
>   --partition 0:1000 --archive s3://atlantis/zarr/my_cube --log-every 50
> ```

Check progress at any time (even after disconnect), from another terminal:

```bash
PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch viirs cube status \
  --partition 0:1000 --db-path cube_tracker.db
PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch modis cube status \
  --partition 0:1000 --db-path modis_cube_tracker.db
```

#### Building both sources into one shared archive

Point both commands at the **same `--archive`** to get a single, multi-source
cube — VIIRS and MODIS each land in their own Zarr group (`viirs`/`modis`)
under one store, sharing the same grid and `ArchiveConfig` (`ATLANTIS_ARCHIVE_ROOT`,
chunk/shard size, scale factor, time epoch — see [`config.py`](../../src/atlantis/config.py)).

This is safe to run **sequentially or concurrently** (e.g. two `tmux` panes),
as long as each run uses its own `--db-path`:

- Every source writes only its own group subtree (`{archive}/viirs/*` vs.
  `{archive}/modis/*`) — there is no shared mutable array state to race on.
- Each run consolidates the store's metadata **once**, when its session
  closes (`ArchiveWriter.session(...).close()`), not per write — a
  best-effort optimisation, never required for correctness of the data
  itself.
- The only edge case is the very first run against a **brand-new, empty**
  archive root, where both processes would create the store for the first
  time — let the first cube build create the store, then start the second
  one (subsequent runs, and runs against an already-initialised archive, are
  fully concurrent-safe).

### 4.2 Build a STAC catalog over the cube

Once the cube is complete, build a static STAC catalog for discovery. Omit
`--source` to catalogue every source group present, or repeat it to select
specific ones:

```bash
# Catalogue every source in the archive (viirs + modis)
PYTHONPATH=src pixi run -e stac python -m atlantis.cli stac build \
  --archive s3://atlantis/zarr/my_cube \
  --output ./data/stac_my_cube \
  --no-compute-bbox

# Or just one source
PYTHONPATH=src pixi run -e stac python -m atlantis.cli stac build \
  --archive s3://atlantis/zarr/my_cube \
  --source modis \
  --output ./data/stac_my_cube_modis \
  --no-compute-bbox
```

`--no-compute-bbox` skips per-date populated-extent computation — **much** faster
for large cubes. Without it, the builder scans every date for non-fill pixels.

### 4.3 Visualise with the time-slider dashboard

`viz serve` takes the source group as a plain argument, so it works
identically for either source:

```bash
PYTHONPATH=src pixi run -e viz python -m atlantis.cli viz serve viirs \
  --stac ./data/stac_my_cube \
  --port 5006

PYTHONPATH=src pixi run -e viz python -m atlantis.cli viz serve modis \
  --stac ./data/stac_my_cube \
  --var recurring_flood \
  --port 5007
```

Open `http://localhost:5006` in a browser (SSH-tunnel if remote: `ssh -L 5006:localhost:5006 <host>`).
`--var recurring_flood` only has meaningful content in the **MODIS** group — see
§6 below.

For tighter AOI and date range:

```bash
PYTHONPATH=src pixi run -e viz python -m atlantis.cli viz serve viirs \
  --stac ./data/stac_my_cube \
  --bbox "-1.5 38.8 0.5 40.0" --start 2024-10-29 --end 2024-11-04 \
  --port 5006
```

---

## 5. Picking a partition — slicing by date

Every catalogue is sorted by a source-specific key before slicing, so
**contiguous date ranges form contiguous row ranges**: VIIRS by
`(date, aoi_id)`, MODIS by `(date, h, v)`. The CLI only takes row slices
(`--partition start:stop`), so compute the row bounds for your date range
up front — **always against the live catalogue**, since row counts shift
every time it is rebuilt or extended (§3.1); a partition table computed today
will be wrong after the next catalogue refresh.

### 5.1 Query the row range for a date span

```python
# VIIRS
from atlantis.fetchers.viirs.inventory import load_inventory
df = load_inventory('s3://atlantis/assets/viirs/viirs_archive_catalog.parquet')
df = df.sort_values(['date', 'aoi_id']).reset_index(drop=True)

# MODIS — same recipe, different sort key
# from atlantis.fetchers.modis.inventory import load_inventory
# df = load_inventory('s3://atlantis/assets/modis/modis_archive_catalog.parquet')
# df = df.sort_values(['date', 'h', 'v']).reset_index(drop=True)

d = df['date'].astype(str)
mask = d.str.startswith('2024-10') | d.str.startswith('2024-11')
subset = df[mask]
start = subset.index[0]
stop = subset.index[-1] + 1   # iloc slice end is exclusive
print(f'Oct-Nov 2024: {len(subset)} rows · partition {start}:{stop}')
```

Run this against whichever catalogue you are about to process — the row
numbers are only valid for that exact Parquet file's current contents.

---

## 6. `recurring_flood` — a MODIS-only layer, present in both groups

MODIS's cube session declares `recurring_flood` in its `var_names` (composite
class `2`, see [`derived.py`](../../src/atlantis/fetchers/modis/derived.py)),
and so does VIIRS's — for schema parity across source groups, so downstream
code can select `recurring_flood` from either group without a `KeyError`.
VIIRS's per-granule payload never populates it, though, so the **VIIRS**
group's `recurring_flood` array stays at the Zarr fill value (nodata) for
every region ever written — by design, not because it is "missing yet."
Treat a MODIS `recurring_flood` read as real data and a VIIRS one as
structurally-always-nodata.

---

## 7. Teardown — deleting a cube and restarting

```bash
# 1. Kill any running batch (either source)
pkill -f "atlantis.cli batch viirs cube"
pkill -f "atlantis.cli batch modis cube"

# 2. Delete the S3 cube (removes every source group under it)
aws s3 rm --recursive s3://atlantis/zarr/my_cube \
  --endpoint-url https://object-store.os-api.cci1.ecmwf.int

# 3. Delete the local trackers (so re-run starts fresh)
rm -f cube_tracker.db modis_cube_tracker.db

# 4. (Optional) Remove local STAC catalog
rm -rf ./data/stac_my_cube
```

---

## 8. Architecture — produce / consume split

The cube build is split into two layers so Zarr metadata consistency is
guaranteed while granule/tile processing stays parallel:

| Layer                                     | Runs on                 | Responsibility                                                        |
| ------------------------------------------- | ----------------------- | --------------------------------------------------------------------- |
| **Produce** — VIIRS: `harmonise_granule_payload`, MODIS: `harmonise_modis_granule_payload` | Dask workers (parallel) | Download granule/tile → classify → harmonise → return payload dict    |
| **Consume** (`ArchiveWriter.session`)     | Coordinator (serial)    | Receive payload → region-write into Zarr → mark task `DONE` in SQLite |

```text
Catalogue (Parquet, per source)
  │
  │  load + slice + to_tasks → [task_1, task_2, …, task_N]
  ▼
Dask LocalCluster
  ├── worker_1  →  harmonise_*_payload(task_1)  →  payload_1
  ├── worker_2  →  harmonise_*_payload(task_2)  →  payload_2
  └── …                                        →  …
  │
  │  as_completed() — stream results as they finish
  ▼
Coordinator (single-threaded, one per source)
  ├── ArchiveWriter.session.write(payload)   # writes into that source's group
  └── mark_done(task_id, output_uri)  →  SQLite tracker (one per source)
```

The coordinator is the bottleneck — but per-granule/tile writes are small (a
few kilobytes each), so the 2–6 workers typically keep it fed. Running VIIRS's
and MODIS's coordinators concurrently is fine — see §4.1.

---

## 9. Runtime estimates

| Granules (VIIRS)            | Approx. runtime (6 workers, ~5–15 min / 100 granules) |
| ----------------------------- | ----------------------------------------------------- |
| 100                          | 5–15 min                                              |
| 1,000                        | 50 min – 2.5 hr                                       |
| 8,855 (Oct–Nov 2024)         | 7–22 hr                                               |
| 53,287 (full 2024)           | 44–133 hr                                             |
| 174,252 (entire catalogue)   | 145–436 hr                                            |

> VIIRS runtime is dominated by NOAA HTTPS download speed (source TIFFs are
> uncompressed one-row strips — pathological for range reads, so each ~20 MB
> granule is downloaded sequentially). Dask dashboard (`http://localhost:8787`)
> shows worker utilisation and per-task timing in real time.

MODIS throughput has not been benchmarked at scale yet — tiles are smaller
(~23 MB uncompressed, fixed grid) but the LAADS/Earthdata endpoint may itself
be rate-limited differently from NOAA's anonymous S3 bucket; treat the VIIRS
table as a rough reference, not a MODIS estimate, until a full run's numbers
are recorded here. MODIS's dashboard defaults to `http://localhost:8788`.
