# Building the Zarr Datacube — Operational Guide

> Step-by-step instructions for building the consolidated, multi-source Zarr v3
> datacube from a per-source granule/tile/cell catalogue using the resume-safe,
> streaming batch pipeline. VIIRS, MODIS, and GFM all plug into the same engine
> and can share one archive.

**Source of truth**

| Concern                                 | Module                                                                                                   |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Cube batch engine                       | [`src/atlantis/archive/cube_batch.py`](../../src/atlantis/archive/cube_batch.py)                         |
| Granule processor (VIIRS)               | [`src/atlantis/fetchers/viirs/batch_processor.py`](../../src/atlantis/fetchers/viirs/batch_processor.py) |
| Tile processor (MODIS)                  | [`src/atlantis/fetchers/modis/batch_processor.py`](../../src/atlantis/fetchers/modis/batch_processor.py) |
| Cell processor (GFM)                    | [`src/atlantis/fetchers/gfm/batch_processor.py`](../../src/atlantis/fetchers/gfm/batch_processor.py)     |
| Inventory loader + tasks                | [`.../viirs/inventory.py`](../../src/atlantis/fetchers/viirs/inventory.py) / [`.../modis/inventory.py`](../../src/atlantis/fetchers/modis/inventory.py) / [`.../gfm/inventory.py`](../../src/atlantis/fetchers/gfm/inventory.py) |
| Catalogue builder                       | [`.../viirs/catalog.py`](../../src/atlantis/fetchers/viirs/catalog.py) / [`.../modis/catalog.py`](../../src/atlantis/fetchers/modis/catalog.py) / [`.../gfm/catalog.py`](../../src/atlantis/fetchers/gfm/catalog.py) |
| Shared catalogue core                   | [`src/atlantis/batch/catalog.py`](../../src/atlantis/batch/catalog.py) (load/slice/write/date-range, reused by every source) |
| CLI (`batch viirs …` / `batch modis …` / `batch gfm …`) | [`src/atlantis/cli.py`](../../src/atlantis/cli.py#L2448)                                  |
| Underlying store layout                 | [`zarr-spec.md`](./zarr-spec.md)                                                                         |

> After building the cube, use the [STAC + Visualization guide](./stac-and-viz.md) to
> catalogue and explore it interactively.

---

## 1. Overview

The batch pipeline converts a per-source catalogue (VIIRS granules, MODIS
tiles, or GFM STAC-item cells) into a **single Zarr v3 datacube**
(`datacube.zarr`) co-registered on the canonical global 1-arcmin grid. Every
source writes into its own group (`viirs`, `modis`, `gfm`, ...) inside the
same store, so one archive can hold as many sources as you build into it. The
pipeline is:

| Property             | How it works                                                                                                                                 |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| **Parallel**         | Dask `LocalCluster` (2–6 adaptive workers). Each worker downloads/streams, classifies, and harmonises one granule/tile/cell at a time.        |
| **Resume-safe**      | SQLite tracker records every `(task_id, status, output_uri)`. Re-running skips already-`DONE` tasks.                                         |
| **Streaming**        | `as_completed()` feeds results into a single coordinator that writes to Zarr — no giant in-RAM accumulation.                                 |
| **Crash-proof**      | Run in `tmux` / `nohup`. Kill at any time; re-run to resume from the tracker.                                                                |
| **Dataset-agnostic** | Same engine (`run_cube_batch`) drives VIIRS (`run_viirs_cube_batch`), MODIS (`run_modis_cube_batch`), and GFM (`run_gfm_cube_batch`) by plugging in a different inventory loader and per-task processor — a fourth source only needs its own thin wrapper. |

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

**VIIRS and GFM need no token** — the NOAA JPSS S3 bucket is public/anonymous,
and the EODC STAC API (`https://stac.eodc.eu/api/v1`) that backs GFM is public too.

---

## 3. Building or refreshing a catalogue

Each source's catalogue is a Parquet inventory of everything available to
ingest — VIIRS granules, MODIS tiles, or GFM STAC items — built once and
re-used (and periodically extended) across cube builds. All three builders
share the same underlying mechanics
([`atlantis/batch/catalog.py`](../../src/atlantis/batch/catalog.py)):
load/slice/write Parquet, walk an inclusive date range, retry a flaky listing
call. They differ only in schema and remote source:

| | VIIRS (`batch viirs catalog`) | MODIS (`batch modis catalog`) | GFM (`batch gfm catalog`) |
| --- | --- | --- | --- |
| Remote source          | NOAA JPSS public S3 bucket (anonymous)         | NASA LAADS DAAC (authenticated)               | EODC STAC API (public, `https://stac.eodc.eu/api/v1`) |
| Auth                   | None                                            | `EARTHDATA_TOKEN` (§2.3)                       | None |
| Output schema          | `date, aoi_id, s3_key, geometry` (GeoParquet)   | `date, h, v, task_id, source_uri` (Parquet)    | `date, equi7_tile, item_id, item_href, west, south, east, north` (Parquet, one row per STAC item) |
| Default `--output`     | `viirs_archive_catalog.parquet` (local)         | `modis_archive_catalog.parquet` (local)        | `gfm_archive_catalog.parquet` (local) |
| Canonical archive path | `s3://atlantis/assets/viirs/viirs_archive_catalog.parquet` | `s3://atlantis/assets/modis/modis_archive_catalog.parquet` | `s3://atlantis/assets/gfm/gfm_archive_catalog.parquet` |

```bash
# VIIRS — no credentials needed
PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch viirs catalog \
  --start 2025-01-01 --end 2025-12-31 \
  --output s3://atlantis/assets/viirs/viirs_archive_catalog.parquet

# MODIS — requires EARTHDATA_TOKEN
PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch modis catalog \
  --start 2025-01-01 --end 2025-12-31 \
  --output s3://atlantis/assets/modis/modis_archive_catalog.parquet

# GFM — no credentials needed, always global (no --bbox option)
PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch gfm catalog \
  --start 2025-01-01 --end 2025-12-31 \
  --output s3://atlantis/assets/gfm/gfm_archive_catalog.parquet
```

> **GFM's catalogue is per-STAC-item, not per-cell.** GFM STAC items already
> carry an `Equi7Tile` id (e.g. `EU020M_E036N009T3`) — a fixed global tile grid
> that plays the same role as MODIS's `(h, v)` — but unlike MODIS/VIIRS, more
> than one item can land on the same `(date, equi7_tile)` cell (e.g. an
> ascending + a descending Sentinel-1 pass on the same day). The catalogue
> keeps one row per item; grouping same-cell items into a single accumulating
> batch task happens later, in `atlantis.fetchers.gfm.inventory.to_tasks`. GFM
> is also considerably denser than VIIRS/MODIS — a small test bbox returned 13
> items over just 6 days — so a full global, full-history (2015–present)
> catalogue will be large; consider a bounded `--start`/`--end` window first
> to gauge size before committing to the full history.

> **Unlike the cube build, the catalogue builder is a plain sequential,
> network-bound loop** — one HTTP request (or STAC query) per calendar day,
> no Dask workers, and **no SQLite resume tracker**, for any source. If it's
> interrupted, that run's progress is gone and you restart from `--start`.
> Building MODIS's full history (2003–2026, ~8,600 days) can take **hours**;
> run it detached (`tmux`/`nohup`) exactly like the cube build:
>
> ```bash
> tmux new -s modis_catalog
> PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch modis catalog \
>   --start 2003-01-01 --end 2026-07-15 \
>   --output s3://atlantis/assets/modis/modis_archive_catalog.parquet
> ```
>
> **Checking progress**: it prints automatically — a line like `MODIS
> catalog: 2400/8597 (27.9%)` every ~30 processed dates — with **no
> `--verbose` flag needed** (it's routed through the CLI's console output,
> not `loguru`, which the CLI disables by default). The same applies
> verbatim to `batch viirs catalog` and `batch gfm catalog` (e.g. `GFM
> catalog: 240/365 (65.8%)`). To confirm a detached run is still alive and
> actually making requests rather than stuck: `pgrep -af "batch <source>
> catalog"` and `ss -tnp | grep <pid>` (look for an `ESTABLISHED` connection
> to the source host).
>
> **GFM is dense enough that a full single-shot history build is risky** —
> see §3.2 for a chunked, incremental build strategy instead of one
> multi-year `batch gfm catalog` invocation.

All three default `--output` to a **bare local filename**, not the canonical
S3 path — pass `-o s3://atlantis/assets/<source>/...` explicitly once you are
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

(Use `subset=["date", "h", "v"]` for MODIS, or
`subset=["date", "equi7_tile", "item_id"]` for GFM — GFM catalogue rows are
per-item, so dedupe on the item id too, not just the cell.)

### 3.2 Building GFM's catalogue incrementally (recommended)

GFM's STAC search is **much denser** than VIIRS/MODIS — a small test bbox
returned 13 items over just 6 days — and, like every catalogue builder, it
has **no resume tracker** (§3). Running one `batch gfm catalog --start
2015-01-01 --end 2026-07-15` invocation for the full history is therefore
risky: hours into the run, any network hiccup, timeout, or killed session
loses everything scanned so far, and you restart from `--start` with
nothing to show for it.

Instead, build the catalogue **year by year** (or in smaller chunks if a
single year is still too slow) into separate local files, then merge:

```bash
# One tmux window per year, or a simple sequential loop — either works.
for year in 2015 2016 2017 2018 2019 2020 2021 2022 2023 2024 2025; do
  PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch gfm catalog \
    --start "${year}-01-01" --end "${year}-12-31" \
    --output "gfm_archive_catalog_${year}.parquet" \
    | tee "gfm_catalog_${year}.log"
done
```

Each year is an **independent invocation** — if one fails partway through,
only that year needs to be re-run; the already-written
`gfm_archive_catalog_<year>.parquet` files for completed years are
untouched. For faster wall-clock time, split the years across several
`tmux` panes (or hosts) instead of one sequential loop — nothing about
`batch gfm catalog` requires the runs to be sequential or on the same
machine, since each year writes its own output file.

**Checking progress** works exactly as described in §3, per year: the `GFM
catalog: N/365 (X.X%)` lines print into each year's own log (or `tmux`
pane), and `pgrep -af "batch gfm catalog"` lists every year's process still
running, so you can see at a glance how many are in flight and — combined
with `ss -tnp | grep <pid>` — confirm each is still making requests rather
than stuck.

Once every year is done, merge the per-year files into one catalogue and
publish it to the canonical S3 path:

```python
import glob

import pandas as pd
from atlantis.fetchers.gfm.inventory import load_inventory

# Merge every per-year chunk built above.
chunks = [pd.read_parquet(p) for p in sorted(glob.glob("gfm_archive_catalog_*.parquet"))]
combined = pd.concat(chunks, ignore_index=True)

# If a canonical catalogue already exists on S3, fold it in too (§3.1) —
# dedupe on the item id, since GFM catalogue rows are per-STAC-item.
try:
    existing = load_inventory("s3://atlantis/assets/gfm/gfm_archive_catalog.parquet")
    combined = pd.concat([existing, combined], ignore_index=True)
except FileNotFoundError:
    pass  # first-ever build — nothing to fold in yet

combined = combined.drop_duplicates(subset=["date", "equi7_tile", "item_id"])
combined = combined.sort_values(["date", "equi7_tile"]).reset_index(drop=True)
combined.to_parquet("gfm_archive_catalog_merged.parquet", index=False)
# then upload gfm_archive_catalog_merged.parquet to
# s3://atlantis/assets/gfm/gfm_archive_catalog.parquet
```

This is the same merge-and-dedupe pattern as §3.1, just applied across many
chunks instead of a single "old + new" pair — extending the catalogue with a
new year later on is exactly the §3.1 workflow, one chunk at a time.

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

# GFM — same --archive, its own tracker, its own row partition
PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch gfm cube run \
  --partition 0:1000 \
  --archive s3://atlantis/zarr/my_cube \
  --db-path gfm_cube_tracker.db \
  --log-every 50
```

| Flag                | VIIRS default                        | MODIS default                       | GFM default                          | Purpose                                |
| ------------------- | ------------------------------------- | ------------------------------------ | ------------------------------------- | --------------------------------------- |
| `--partition`       | full catalogue                        | full catalogue                       | full catalogue                        | Row slice `start:stop` (e.g. `0:1000`) — for GFM this slices STAC-item rows *before* the `(date, equi7_tile)` grouping (see §5) |
| `--archive` / `-a`  | `s3://atlantis/zarr/viirs_2020_cube`  | `s3://atlantis/zarr/modis_cube`      | `s3://atlantis/zarr/gfm_cube`         | Cube root — a local dir or `s3://` URI |
| `--log-every`       | `100`                                  | `50`                                  | `50`                                  | Progress line every N completions      |
| `--workers-min/max` | `2` / `6`                              | `2` / `6`                             | `2` / `6`                             | Dask worker count (adaptive)           |
| `--memory-limit`    | `4GB`                                  | `2.5GB`                               | `4GB`                                 | Memory cap per worker — GFM loads a full 15000×15000 EQUI7 tile at native ~20 m resolution before coarsening, so it needs VIIRS-level headroom |
| `--dashboard-port`  | `8787`                                 | `8788`                                | `8789`                                | Distinct ports so all three dashboards can run at once |
| `--db-path`         | `cube_tracker.db`                      | `cube_tracker.db`                     | `gfm_cube_tracker.db`                 | SQLite resume database — **use a different path per source** when writing into the same archive concurrently |
| `--retries`         | `3`                                    | `3`                                   | `3`                                   | Retries per granule/tile/cell          |
| `--composite`       | n/a                                    | `None` (→ `F2`)                      | n/a                                    | MODIS-only: MCDWD composite to extract (`F1`/`F1C`/`F2`/`F3`) |
| `--gfm-coarsen-factor` / `--gfm-resampling` | n/a                    | n/a                                   | `4` / `average`                       | GFM-only: spatial coarsening factor and resampling method before reprojection (overrides `ATLANTIS_GFM_COARSEN_FACTOR` / `ATLANTIS_GFM_RESAMPLING`) |

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
PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch gfm cube status \
  --partition 0:1000 --db-path gfm_cube_tracker.db
```

#### Building all three sources into one shared archive

Point every command at the **same `--archive`** to get a single, multi-source
cube — VIIRS, MODIS, and GFM each land in their own Zarr group (`viirs` /
`modis` / `gfm`) under one store, sharing the same grid and `ArchiveConfig`
(`ATLANTIS_ARCHIVE_ROOT`, chunk/shard size, scale factor, time epoch — see
[`config.py`](../../src/atlantis/config.py)).

This is safe to run **sequentially or concurrently** (e.g. three `tmux`
panes), as long as each run uses its own `--db-path`:

- Every source writes only its own group subtree (`{archive}/viirs/*` vs.
  `{archive}/modis/*` vs. `{archive}/gfm/*`) — there is no shared mutable
  array state to race on.
- Each run consolidates the store's metadata **once**, when its session
  closes (`ArchiveWriter.session(...).close()`), not per write — a
  best-effort optimisation, never required for correctness of the data
  itself.
- The only edge case is the very first run against a **brand-new, empty**
  archive root, where multiple processes would race to create the store for
  the first time — let the first cube build create the store, then start the
  others (subsequent runs, and runs against an already-initialised archive,
  are fully concurrent-safe).

**Adding GFM to an existing VIIRS+MODIS archive** needs no migration or
special handling — it follows exactly the same rule as above, since the
archive root you already built is *not* a brand-new/empty one:

```bash
PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch gfm cube run \
  --partition 0:1000 \
  --archive s3://atlantis/zarr/my_cube \
  --db-path gfm_cube_tracker.db \
  --log-every 50
```

This creates a new `gfm` group alongside the existing `viirs`/`modis` groups
in the same store — it does not touch, rewrite, or resize anything already
written for the other two sources. Give it its own `--db-path` (as shown) so
its resume tracker doesn't collide with the VIIRS/MODIS trackers, and it's
safe to run while VIIRS/MODIS builds are still catching up on the same
archive.

### 4.2 Build a STAC catalog over the cube

Once the cube is complete, build a static STAC catalog for discovery. Omit
`--source` to catalogue every source group present, or repeat it to select
specific ones. `--source` discovers whatever Zarr groups actually exist in
the archive (`ArchiveReader.list_sources()`), so adding GFM to an archive that
already has `viirs`/`modis` groups needs no code or flag changes here — it is
picked up automatically the next time you (re)build the catalog:

```bash
# Catalogue every source in the archive (viirs + modis + gfm)
PYTHONPATH=src pixi run -e stac python -m atlantis.cli stac build \
  --archive s3://atlantis/zarr/my_cube \
  --output ./data/stac_my_cube \
  --no-compute-bbox

# Or just one source
PYTHONPATH=src pixi run -e stac python -m atlantis.cli stac build \
  --archive s3://atlantis/zarr/my_cube \
  --source gfm \
  --output ./data/stac_my_cube_gfm \
  --no-compute-bbox
```

`--no-compute-bbox` skips per-date populated-extent computation — **much** faster
for large cubes. Without it, the builder scans every date for non-fill pixels.

### 4.3 Visualise with the time-slider dashboard

`viz serve` takes the source group as a plain argument, so it works
identically for any source, including GFM:

```bash
PYTHONPATH=src pixi run -e viz python -m atlantis.cli viz serve viirs \
  --stac ./data/stac_my_cube \
  --port 5006

PYTHONPATH=src pixi run -e viz python -m atlantis.cli viz serve modis \
  --stac ./data/stac_my_cube \
  --var recurring_flood \
  --port 5007

PYTHONPATH=src pixi run -e viz python -m atlantis.cli viz serve gfm \
  --stac ./data/stac_my_cube \
  --port 5008
```

Open `http://localhost:5006` in a browser (SSH-tunnel if remote: `ssh -L 5006:localhost:5006 <host>`).
`--var recurring_flood` only has meaningful content in the **MODIS** group — see
§6 below; GFM's cube group doesn't declare `recurring_flood` at all (see §6).

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
`(date, aoi_id)`, MODIS by `(date, h, v)`, GFM by `(date, equi7_tile)`. The
CLI only takes row slices (`--partition start:stop`), so compute the row
bounds for your date range up front — **always against the live catalogue**,
since row counts shift every time it is rebuilt or extended (§3.1); a
partition table computed today will be wrong after the next catalogue
refresh.

> **GFM caveat**: `--partition` slices the catalogue at individual STAC-item
> granularity, *before* `to_tasks()` groups rows into `(date, equi7_tile)`
> batch tasks (§3). A cell whose items straddle a partition boundary will
> only partially accumulate on each side of the split. This is a low-impact,
> known limitation — pick partition boundaries on whole-date boundaries where
> possible to avoid it in practice.

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

# GFM — same recipe, sorted by (date, equi7_tile); note the row count here is
# per STAC item, not per (date, tile) batch task — see the §5 caveat above.
# from atlantis.fetchers.gfm.inventory import load_inventory
# df = load_inventory('s3://atlantis/assets/gfm/gfm_archive_catalog.parquet')
# df = df.sort_values(['date', 'equi7_tile']).reset_index(drop=True)

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

**GFM's cube group doesn't declare `recurring_flood` at all** — selecting it
from `gfm` raises `KeyError`, unlike the VIIRS/MODIS parity above. GFM's own
`reference_water` layer already folds the seasonal/permanent split into its
native 3-class codes (`0` = no water, `1` = permanent, `2` = seasonal — the
seasonal class is the GFM analog of MODIS's `recurring_flood`), so there was
no separate channel to add for schema parity. See the
[layer reference](../layers.md#layers-cross-source) for the full cross-source
comparison, including `exclusion_mask`, which is a clean binary `0`/`1` mask
for VIIRS/MODIS but GFM's own native, multi-valued SAR codes (`nodata=255`)
passed through untouched.

---

## 7. Teardown — deleting a cube and restarting

```bash
# 1. Kill any running batch (any source)
pkill -f "atlantis.cli batch viirs cube"
pkill -f "atlantis.cli batch modis cube"
pkill -f "atlantis.cli batch gfm cube"

# 2. Delete the S3 cube (removes every source group under it)
aws s3 rm --recursive s3://atlantis/zarr/my_cube \
  --endpoint-url https://object-store.os-api.cci1.ecmwf.int

# 3. Delete the local trackers (so re-run starts fresh)
rm -f cube_tracker.db modis_cube_tracker.db gfm_cube_tracker.db

# 4. (Optional) Remove local STAC catalog
rm -rf ./data/stac_my_cube
```

---

## 8. Architecture — produce / consume split

The cube build is split into two layers so Zarr metadata consistency is
guaranteed while granule/tile processing stays parallel:

| Layer                                     | Runs on                 | Responsibility                                                        |
| ------------------------------------------- | ----------------------- | --------------------------------------------------------------------- |
| **Produce** — VIIRS: `harmonise_granule_payload`, MODIS: `harmonise_modis_granule_payload`, GFM: `harmonise_gfm_payload` | Dask workers (parallel) | Download/stream granule/tile/cell → classify → harmonise → return payload dict |
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

The coordinator is the bottleneck — but per-granule/tile/cell writes are small
(a few kilobytes each), so the 2–6 workers typically keep it fed. Running
VIIRS's, MODIS's, and GFM's coordinators concurrently is fine — see §4.1.

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

GFM throughput has not been benchmarked at scale yet either, and its per-task
cost is shaped differently from VIIRS/MODIS: each task streams a full native
~20 m EQUI7 tile (15000×15000 px) via `odc.stac` HTTP range requests rather
than downloading a single compact file, and a task can cover more than one
STAC item when several Sentinel-1 passes share a `(date, equi7_tile)` cell
(§3). GFM's dashboard defaults to `http://localhost:8789`.
