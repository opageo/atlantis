# Building the Zarr Datacube — Operational Guide

> Step-by-step instructions for building the consolidated Zarr v3 datacube from a
> VIIRS granule catalogue using the resume-safe, streaming batch pipeline.

**Source of truth**

| Concern                    | Module                                                                                                   |
| -------------------------- | -------------------------------------------------------------------------------------------------------- |
| Cube batch engine          | [`src/atlantis/archive/cube_batch.py`](../../src/atlantis/archive/cube_batch.py)                         |
| Granule processor (VIIRS)  | [`src/atlantis/fetchers/viirs/batch_processor.py`](../../src/atlantis/fetchers/viirs/batch_processor.py) |
| Inventory loader + tasks   | [`src/atlantis/fetchers/viirs/inventory.py`](../../src/atlantis/fetchers/viirs/inventory.py)             |
| CLI (`batch viirs cube …`) | [`src/atlantis/cli.py`](../../src/atlantis/cli.py#L2556)                                                 |
| Underlying store layout    | [`zarr-spec.md`](./zarr-spec.md)                                                                         |

> After building the cube, use the [STAC + Visualization guide](./stac-and-viz.md) to
> catalogue and explore it interactively.

---

## 1. Overview

The batch pipeline converts a catalogue of NOAA VIIRS granules into a **single
Zarr v3 datacube** (`datacube.zarr`) co-registered on the canonical global
1-arcmin grid. The pipeline is:

| Property             | How it works                                                                                                                                 |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| **Parallel**         | Dask `LocalCluster` (2–6 adaptive workers). Each worker downloads, classifies, and harmonises one granule at a time.                         |
| **Resume-safe**      | SQLite tracker records every `(task_id, status, output_uri)`. Re-running skips already-`DONE` tasks.                                         |
| **Streaming**        | `as_completed()` feeds results into a single coordinator that writes to Zarr — no giant in-RAM accumulation.                                 |
| **Crash-proof**      | Run in `tmux` / `nohup`. Kill at any time; re-run to resume from the tracker.                                                                |
| **Dataset-agnostic** | Same engine (`run_cube_batch`) drives VIIRS, MODIS, or any future source by plugging in a different inventory loader and per-task processor. |

---

## 2. Prerequisites

### 2.1 AWS / ECMWF object store credentials

The default catalogue lives on `s3://atlantis/` (ECMWF object store). Run once:

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

---

## 3. Full workflow — cube, catalogue, visualise

The three-step end-to-end pipeline:

### 3.1 Build the cube

```bash
PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch viirs cube run \
  --partition 0:1000 \
  --archive s3://atlantis/zarr/my_cube \
  --log-every 50
```

| Flag                | Default                              | Purpose                                |
| ------------------- | ------------------------------------ | -------------------------------------- |
| `--partition`       | full catalogue                       | Row slice `start:stop` (e.g. `0:1000`) |
| `--archive` / `-a`  | `s3://atlantis/zarr/viirs_2020_cube` | Cube root — a local dir or `s3://` URI |
| `--log-every`       | `100`                                | Progress line every N completions      |
| `--workers-min/max` | `2` / `6`                            | Dask worker count (adaptive)           |
| `--memory-limit`    | `4GB`                                | Memory cap per worker                  |
| `--db-path`         | `cube_tracker.db`                    | SQLite resume database                 |
| `--retries`         | `3`                                  | Retries per granule                    |

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
```

### 3.2 Build a STAC catalog over the cube

Once the cube is complete, build a static STAC catalog for discovery:

```bash
PYTHONPATH=src pixi run -e stac python -m atlantis.cli stac build \
  --archive s3://atlantis/zarr/my_cube \
  --output ./data/stac_my_cube \
  --no-compute-bbox
```

`--no-compute-bbox` skips per-date populated-extent computation — **much** faster
for large cubes. Without it, the builder scans every date for non-fill pixels.

### 3.3 Visualise with the time-slider dashboard

```bash
PYTHONPATH=src pixi run -e viz python -m atlantis.cli viz serve viirs \
  --stac ./data/stac_my_cube \
  --port 5006
```

Open `http://localhost:5006` in a browser (SSH-tunnel if remote: `ssh -L 5006:localhost:5006 <host>`).

For tighter AOI and date range:

```bash
PYTHONPATH=src pixi run -e viz python -m atlantis.cli viz serve viirs \
  --stac ./data/stac_my_cube \
  --bbox "-1.5 38.8 0.5 40.0" --start 2024-10-29 --end 2024-11-04 \
  --port 5006
```

---

## 4. Picking a partition — slicing by date

The catalogue (`s3://atlantis/assets/viirs/viirs_archive_catalog.parquet`) is
sorted by `(date, aoi_id)`, so **contiguous date ranges form contiguous row
ranges**. The CLI only takes row slices (`--partition start:stop`), so you need
to compute the row bounds for your date range up front.

### 4.1 Query the row range for a date span

```python
from atlantis.fetchers.viirs.inventory import load_inventory
df = load_inventory('s3://atlantis/assets/viirs/viirs_archive_catalog.parquet')
df = df.sort_values(['date', 'aoi_id']).reset_index(drop=True)
d = df['date'].astype(str)
mask = d.str.startswith('2024-10') | d.str.startswith('2024-11')
subset = df[mask]
start = subset.index[0]
stop = subset.index[-1] + 1   # iloc slice end is exclusive
print(f'Oct-Nov 2024: {len(subset)} granules · partition {start}:{stop}')
```

### 4.2 Pre-computed date ranges (VIIRS JPSS 2020 catalogue, 174,252 rows)

| Period       | Granules | Partition       |
| ------------ | -------- | --------------- |
| Oct 2024     | ~4,442   | `109152:113594` |
| Nov 2024     | ~4,413   | `113594:118007` |
| Oct–Nov 2024 | ~8,855   | `109152:118007` |
| Full 2024    | ~53,287  | `75968:129255`  |
| Full 2020    | ~47,721  | `0:47721`       |

---

## 5. Teardown — deleting a cube and restarting

```bash
# 1. Kill any running batch
pkill -f "atlantis.cli batch viirs cube"

# 2. Delete the S3 cube
aws s3 rm --recursive s3://atlantis/zarr/my_cube \
  --endpoint-url https://object-store.os-api.cci1.ecmwf.int

# 3. Delete the local tracker (so re-run starts fresh)
rm -f cube_tracker.db

# 4. (Optional) Remove local STAC catalog
rm -rf ./data/stac_my_cube
```

---

## 6. Architecture — produce / consume split

The cube build is split into two layers so Zarr metadata consistency is
guaranteed while granule processing stays parallel:

| Layer                                     | Runs on                 | Responsibility                                                        |
| ----------------------------------------- | ----------------------- | --------------------------------------------------------------------- |
| **Produce** (`harmonise_granule_payload`) | Dask workers (parallel) | Download NOAA granule → classify → harmonise → return payload dict    |
| **Consume** (`ArchiveWriter.session`)     | Coordinator (serial)    | Receive payload → region-write into Zarr → mark task `DONE` in SQLite |

```text
Catalogue (Parquet)
  │
  │  load + slice + to_tasks → [task_1, task_2, …, task_N]
  ▼
Dask LocalCluster
  ├── worker_1  →  process_granule(task_1)  →  payload_1
  ├── worker_2  →  process_granule(task_2)  →  payload_2
  └── …                                    →  …
  │
  │  as_completed() — stream results as they finish
  ▼
Coordinator (single-threaded)
  ├── ArchiveWriter.session.write(payload)
  └── mark_done(task_id, output_uri)  →  SQLite tracker
```

The coordinator is the bottleneck — but per-granule writes are small (a few
kilobytes each), so the 2–6 workers typically keep it fed.

---

## 7. Runtime estimates

| Granules                   | Approx. runtime (6 workers, ~5–15 min / 100 granules) |
| -------------------------- | ----------------------------------------------------- |
| 100                        | 5–15 min                                              |
| 1,000                      | 50 min – 2.5 hr                                       |
| 8,855 (Oct–Nov 2024)       | 7–22 hr                                               |
| 53,287 (full 2024)         | 44–133 hr                                             |
| 174,252 (entire catalogue) | 145–436 hr                                            |

> Runtime is dominated by NOAA HTTPS download speed (source TIFFs are uncompressed
> one-row strips — pathological for range reads, so each ~20 MB granule is
> downloaded sequentially). Dask dashboard (`http://localhost:8787`) shows worker
> utilisation and per-task timing in real time.
