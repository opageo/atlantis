# Event GFM Archives — GEOID-Flood & KuroSiwo

> How the GEOID-Flood and KuroSiwo event sets are processed into the Atlantis
> GFM Zarr archives: the exact commands, the task model, the two archive
> targets, and how to re-run everything for reproducibility.

**Source of truth**

| Concern                             | Module                                                                                                                                                              |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| GEOID-Flood archive build script    | [`scripts/build_geoidflood_gfm_archive.py`](../../scripts/build_geoidflood_gfm_archive.py)                                                                          |
| KuroSiwo archive build script       | [`scripts/build_kurosiwo_gfm_archive.py`](../../scripts/build_kurosiwo_gfm_archive.py)                                                                              |
| Shared task building                | [`src/atlantis/fetchers/gfm/event_tasks.py`](../../src/atlantis/fetchers/gfm/event_tasks.py)                                                                        |
| Batch engine (`run_gfm_cube_batch`) | [`src/atlantis/archive/cube_batch.py`](../../src/atlantis/archive/cube_batch.py)                                                                                    |
| AOI metadata tables (generated)     | `data/metadata/geoidflood_aois.csv` · `data/metadata/kurosiwo_aois.csv`                                                                                             |
| AOI estimation scripts              | [`scripts/estimate_geoidflood_aois.py`](../../scripts/estimate_geoidflood_aois.py) · [`scripts/estimate_kurosiwo_aois.py`](../../scripts/estimate_kurosiwo_aois.py) |
| AOI derivation                      | [`src/atlantis/utils/geoidflood.py`](../../src/atlantis/utils/geoidflood.py) · [`src/atlantis/utils/kurosiwo.py`](../../src/atlantis/utils/kurosiwo.py)             |
| Event metadata (AOI table inputs)   | `data/metadata/geoidflood_metadata_v1.csv` · `data/metadata/kurosiwo_metadata_v1.csv`                                                                               |
| Cached task lists                   | `data/benchmark/gfm_aoi_tasks_geoidflood_all.json` · `data/benchmark/gfm_aoi_tasks_all.json` (KuroSiwo)                                                             |
| pixi tasks                          | [`pixi.toml`](../../pixi.toml) (`build-*-gfm-archive`, `backfill-*-gfm`)                                                                                            |
| Underlying store layout             | [`zarr-spec.md`](./zarr-spec.md)                                                                                                                                    |

> Only the **GFM** source is processed for these event sets. VIIRS/MODIS groups
> in the yearly cubes are produced by the regular cube build
> ([`cube-build.md`](./cube-build.md)) and are untouched by these scripts.

---

## 1. Overview

Both event programs — **GEOID-Flood** (EMSR activations and related events)
and **KuroSiwo** (SAR flood catalogue cases) — are processed the same way: an
AOI metadata table defines each (event, AoI) with a date window (metadata
range + 14-day post-flood pad), and one **GFM batch task** is generated per
**(event-AoI, date, EQUI7 tile)** inside that window. Tasks are streamed
through the production GFM cube batch engine into a Zarr cube with the
standard archive schema, so the existing reader, STAC and viz tooling work
unchanged.

| Property      | How it works                                                                                                                                                                                                           |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Task unit     | One EQUI7 tile for one date — GFM's native storage unit (one STAC item per (tile, date), streamed as a whole COG from EODC). Each task carries the tile's own bbox; events only select which tiles/dates are in scope. |
| Task sources  | Per-year S3 catalogues where they exist (2021–2025, offline) + live EODC STAC searches day-by-day for the rest of the window (GEOID-Flood spans 2016–2026).                                                            |
| Parallel      | Dask `LocalCluster` (2–6 adaptive workers) via `BatchConfig`.                                                                                                                                                          |
| Resume-safe   | SQLite tracker records every task; re-running skips `DONE` tasks.                                                                                                                                                      |
| Validation    | Post-run check: every task date present on the gfm `time` axis, axis strictly ascending.                                                                                                                               |
| Dropped items | STAC items missing a valid `Equi7Tile`/bbox are recorded to `<tasks>.dropped.json` for post-run coverage reconciliation.                                                                                               |

Task ids embed the event, AoI, tile and date
(`gfm-EMSR712-10-EU020M_E036N009T3-20241029`), so two AoIs of one activation
never collide in the tracker.

---

## 2. Event model & AOI definitions

Neither program archives the source datasets' own imagery. The source
catalogue is used only to derive a **bbox + date window** per AOI; the
imagery it describes (KuroSiwo's exported SAR patches, GEOID-Flood's
benchmark tiles) is never downloaded or stored. What lands in the cube is
the **Copernicus GFM product** — the daily Sentinel-1-derived flood layers —
wherever and whenever it exists inside that window. The two programs differ
in what an AOI is, and therefore in how those bboxes and windows are
derived.

### 2.1 GEOID-Flood: one AOI per (activation, tile-group)

GEOID-Flood is a multi-modal benchmark built from Copernicus EMS Rapid
Mapping activations (`EMSR151`–`EMSR871`). Its only spatial metadata is the
Hugging Face `tile_catalog.parquet` (main + held-out trees merged): one row
per 1024×1024 tile, with per-tile WKB geometry (EPSG:4326) and Sentinel-1
`delineation_time_pre` / `delineation_time_post` acquisition times. There is
no per-event AOI geometry file, so the AOIs are derived by grouping tiles
([`src/atlantis/utils/geoidflood.py`](../../src/atlantis/utils/geoidflood.py),
`derive_geoidflood_metadata`, run on the fly by the estimate script if the
metadata CSV is missing):

- **AOI unit** — the benchmark's native AoI: `tile_id`, the `N` in
  `EMSR712-N`. An activation (event) has multiple AOIs.
- **bbox** — union of the AoI's tile geometries (EPSG:4326).
- **date window** — span of the AoI's Sentinel-1 delineation acquisitions:
  `date_start = min(delineation_time_pre)`,
  `date_end = max(delineation_time_post)`.
- Corrupt tile rows are dropped when deriving (missing delineation times,
  `post < pre`, post year > 2027).

[`scripts/estimate_geoidflood_aois.py`](../../scripts/estimate_geoidflood_aois.py)
then adds the 14-day post-flood pad and writes `data/metadata/geoidflood_aois.csv`
(one row per event-AoI: `event_id`, `aoi_id`, bbox, `date_start`, `date_end`,
`n_dates`). Windows span 2016–2026, so most of the set falls outside the
catalogue years and must be searched live (§2.4).

### 2.2 KuroSiwo: one AOI per flood case

KuroSiwo is a SAR flood catalogue (`assets/ks_catalogue.gpkg`): one row per
exported SAR patch, organised as Event (`actid`) → 224×224 tiles → patches.
[`src/atlantis/utils/kurosiwo.py`](../../src/atlantis/utils/kurosiwo.py)
(`derive_kurosiwo_metadata`, exposed as `atlantis.cli build-kurosiwo-metadata`)
reduces it to one row per flood case (43 in the v1 catalogue):

- **AOI unit** — the whole event: `flood_case = KuroSiwo_{actid:03d}`, and
  the AOI table sets `aoi_id = flood_case` (exactly one AOI per event).
- **bbox** — `total_bounds` of **all** catalogued patch geometries
  (pre-flood and flood-time) reprojected to WGS84: the SAR tile footprint,
  **not** the inundated extent.
- **date window** — SAR acquisition dates, not hydrological event bounds:
  `date_start` = earliest `source_date` of the pre-flood (`master=False`)
  acquisitions (the oldest baseline image), `date_end` = earliest
  flood-time (`master=True`) `source_date` (the flood image date). Most
  events have exactly one flood-time acquisition, so there is no
  multi-temporal flood timeline.

[`scripts/estimate_kurosiwo_aois.py`](../../scripts/estimate_kurosiwo_aois.py)
adds the same 14-day pad and writes `data/metadata/kurosiwo_aois.csv`.
Windows span 2014–2022, so only 2021–2022 overlap the catalogue years.

The metadata also carries extent fields (`max_flood_extent_km2`, static
`pflood` labels) — `pflood` is a per-tile training label identical across
pre- and flood-time acquisitions, not a per-date signal. None of these are
used by the archive build; only bbox and dates matter.

### 2.3 Differences at a glance

| Aspect                | GEOID-Flood                                    | KuroSiwo                                                        |
| --------------------- | ---------------------------------------------- | --------------------------------------------------------------- |
| AOI unit              | one per (activation, tile-group): `EMSR712-10` | one per event: `KuroSiwo_{actid:03d}` (`aoi_id` = `flood_case`) |
| Source catalogue      | HF `tile_catalog.parquet` (1024×1024 tiles)    | KuroSiwo `catalogue.gpkg` (224×224 tiles / patches)             |
| bbox                  | union of the AoI's tile geometries             | `total_bounds` of all SAR patch footprints                      |
| date window (pre-pad) | min pre → max post delineation acquisitions    | oldest pre-flood → earliest flood-time acquisition              |
| Window years          | 2016–2026                                      | 2014–2022                                                       |
| Task id               | embeds event **and** AoI (`gfm-EMSR712-10-…`)  | embeds AoI = event (`gfm-KuroSiwo_118-…`)                       |

Both get the +14-day post-flood pad, and in both the bbox only selects which
EQUI7 tiles/dates are in scope.

### 2.4 Whole AOI, GFM-dependent coverage

- **The full AOI bbox × full window is processed** — nothing is clipped to
  the flood mask or the inundated footprint. The AOI bbox feeds a
  bbox-intersects query per day (live search) or a catalogue bbox filter,
  and every intersecting EQUI7 tile/date becomes a task. The AOI is a
  **tile-selection filter, not a pixel-level clip**: intersecting tiles are
  archived whole, and tiles that don't intersect the AOI are simply never
  fetched — reading the cube with a wider bbox shows nothing there.
- **The archive content is whatever GFM exists.** GFM's storage unit is one
  STAC item per (EQUI7 tile, date) — a daily, Sentinel-1-derived product —
  so coverage is driven entirely by GFM availability, **not** by the source
  event's own imagery (which only defines bboxes/windows and is never
  archived). Days without GFM items produce no tasks and leave empty time
  slots.
- **Default destination is the yearly cube.** These per-event archives
  region-write the gfm group of `s3://atlantis/zarr/{YYYY}/datacube.zarr`
  (§4, §5.3); the dedicated `geoidflood_events` / `kurosiwo_events` cubes
  are the alternative single-store layout.
- Catalogue years (2021–2025) are built offline; every other window day is a
  live EODC STAC search day-by-day. GEOID-Flood's pre-2021 windows are
  searched live and expected empty, so those events archive little or
  nothing; KuroSiwo's 2014–2020 windows likewise, with only the 2021–2022
  portion hitting catalogues.

---

## 3. Prerequisites

- **ECMWF object-store credentials** — the `default` AWS profile with the EODC
  endpoint, configured once via `pixi run setup` (see
  [`cube-build.md`](./cube-build.md#21-aws--ecmwf-object-store-credentials)).
  Verify with:

  ```bash
  aws s3 ls s3://atlantis/zarr/ --endpoint-url https://object-store.os-api.cci1.ecmwf.int
  ```

- **`events` pixi environment** — `pixi install -e events`.
- **Run detached** — every run is long; use `tmux` so an SSH disconnect does
  not kill the coordinator.

---

## 4. Archive targets

Two layouts are supported, both producing the standard
[`datacube.zarr` schema](./zarr-spec.md):

1. **Dedicated event cube** — one store for the whole program, time axis grows
   by data as events are added:
   `s3://atlantis/zarr/geoidflood_events/datacube.zarr` /
   `s3://atlantis/zarr/kurosiwo_events/datacube.zarr`.
2. **Per-year backfill (default workflow)** — events region-write into the
   **yearly cubes** at `s3://atlantis/zarr/{YYYY}/datacube.zarr`. `--year`
   pre-fills the gfm group's `time` axis with all 366 days of the year so any
   event date lands in a pre-existing slot — no ordering constraint, no
   reindex, and re-running an event **overwrites its cells in place**
   (corrections). Events straddling a year boundary are split across two runs
   (one `--year` each).

Only GFM data is written; other groups in the yearly cubes are left alone.

---

## 5. Commands

The scripts are exposed as `pixi run -e events` tasks:

| Task                           | Wraps                                                               |
| ------------------------------ | ------------------------------------------------------------------- |
| `build-geoidflood-gfm-archive` | `PYTHONPATH=src python scripts/build_geoidflood_gfm_archive.py`     |
| `build-kurosiwo-gfm-archive`   | `PYTHONPATH=src python scripts/build_kurosiwo_gfm_archive.py`       |
| `backfill-geoidflood-gfm`      | `…/build_geoidflood_gfm_archive.py --year {year} --events {events}` |
| `backfill-kurosiwo-gfm`        | `…/build_kurosiwo_gfm_archive.py --year {year} --events {events}`   |

Every task accepts the underlying script's flags (`--archive`, `--year`,
`--events`, `--db-path`, `--workers`, `--memory-limit`, `--tasks-only`,
`--tasks`).

### 5.1 Process **all** events (GEOID-Flood)

One command builds the complete task list (catalogues + live search), caches
it, and streams every task into the dedicated event cube:

```bash
tmux new -s geoidflood
PYTHONPATH=src pixi run -e events build-geoidflood-gfm-archive \
    --archive s3://atlantis/zarr/geoidflood_events \
    --db-path geoidflood_gfm_cube_tracker.db
```

### 5.2 Process **all** events (KuroSiwo)

```bash
tmux new -s kurosiwo
PYTHONPATH=src pixi run -e events build-kurosiwo-gfm-archive \
    --archive s3://atlantis/zarr/kurosiwo_events \
    --db-path kurosiwo_gfm_cube_tracker.db
```

Re-running either command resumes from the SQLite tracker (`--db-path`):
already-`DONE` tasks are skipped.

> **Task-list caching.** The full task list is built once and cached to
> `data/benchmark/gfm_aoi_tasks_geoidflood_all.json` /
> `data/benchmark/gfm_aoi_tasks_all.json`. To only (re)generate the task list
> without running the batch — e.g. to inspect scope before committing to a
> run — add `--tasks-only`; with `--events`/`--year` it also writes the
> filtered sidecar list.

### 5.3 Per-event backfill into the yearly cubes

The **default workflow** is backfilling individual events into the per-year
cubes (`s3://atlantis/zarr/{YYYY}`), typically as new events arrive:

```bash
# GEOID-Flood: activation EMSR712, AoI 10, 2025
tmux new -s backfill_EMSR712
PYTHONPATH=src pixi run -e events backfill-geoidflood-gfm \
    --year 2025 --events EMSR712-10 --db-path backfill_EMSR712_2025.db
```

or with the raw task:

```bash
PYTHONPATH=src pixi run -e events build-geoidflood-gfm-archive \
    --year 2025 --events EMSR712-10 --db-path backfill_EMSR712_2025.db
```

KuroSiwo works identically:

```bash
PYTHONPATH=src pixi run -e events backfill-kurosiwo-gfm \
    --year 2025 --events BGD-2024-000223-FIN --db-path backfill_BGD_2025.db
```

- `--events` accepts comma-separated **event ids** or **event-AoI combos**
  (`EMSR712-10`); an unknown id fails loudly with the available event ids.
- **Build the full task cache before filtered backfills.** The shared task
  list (`data/benchmark/gfm_aoi_tasks_geoidflood_all.json` /
  `gfm_aoi_tasks_all.json`) is loaded and reused even for filtered runs. If it
  does not exist yet, a filtered run generates only the requested subset and
  writes it to the shared path; the next filtered run then loads that subset
  and silently finds "No tasks match" — nothing is processed. Populate the
  cache first with an unfiltered `--tasks-only` run (or the §5.1/§5.2
  all-events build), or give each backfill its own `--tasks` path.
- `--year` sets the archive to `s3://atlantis/zarr/{YYYY}` (overriding
  `--archive`), filters tasks to that calendar year, and triggers the
  **time-axis prefill** described in §4.
- A run that straddles a year boundary must be split into one invocation per
  year (the `--year` filter does the splitting; run each separately with its
  own tracker, or the full-event command without `--year`).
- **Corrections / re-runs are safe**: `DONE` tasks are skipped and re-running
  an event overwrites its cells in place.

### 5.4 Running both programs

The two programs are independent — separate scripts, task lists, trackers,
and archive targets. Run them in separate `tmux` sessions (their Dask
dashboard ports differ: `8796` GEOID-Flood, `8795` KuroSiwo) or sequentially
with distinct `--db-path` values.

### 5.5 Examples — the smallest events from each collection

The three smallest GEOID-Flood and three smallest KuroSiwo
events by AOI/date-range (window = metadata range; pad end = window + 14-day
post-flood pad). All are single-year, so each is one backfill command into
`s3://atlantis/zarr/{YYYY}/datacube.zarr`:

| Program     | Event-AoI          | Window                    | Pad end    | Year | Task source    |
| ----------- | ------------------ | ------------------------- | ---------- | ---- | -------------- |
| GEOID-Flood | `EMSR864-21`       | 2026-03-01 → 03-15 (15 d) | 2026-03-29 | 2026 | live search    |
| GEOID-Flood | `EMSR184-4`        | 2016-09-23 → 10-09 (17 d) | 2016-10-23 | 2016 | live search    |
| GEOID-Flood | `EMSR292-1`        | 2018-06-25 → 07-13 (19 d) | 2018-07-27 | 2018 | live search    |
| KuroSiwo    | `KuroSiwo_1111003` | 2019-11-09 → 12-11 (33 d) | 2019-12-25 | 2019 | live search    |
| KuroSiwo    | `KuroSiwo_498`     | 2021-01-15 → 02-16 (33 d) | 2021-03-02 | 2021 | 2021 catalogue |
| KuroSiwo    | `KuroSiwo_1111011` | 2020-10-20 → 11-27 (39 d) | 2020-12-11 | 2020 | live search    |

```bash
# GEOID-Flood
tmux new -s gfb_EMSR864_2026
pixi run -e events backfill-geoidflood-gfm --year 2026 --events EMSR864-21 --db-path backfill_EMSR864_2026.db

tmux new -s gfb_EMSR184_2016
pixi run -e events backfill-geoidflood-gfm --year 2016 --events EMSR184-4 --db-path backfill_EMSR184_2016.db

tmux new -s gfb_EMSR292_2018
pixi run -e events backfill-geoidflood-gfm --year 2018 --events EMSR292-1 --db-path backfill_EMSR292_2018.db

# KuroSiwo
tmux new -s ks_1111003_2019
pixi run -e events backfill-kurosiwo-gfm --year 2019 --events KuroSiwo_1111003 --db-path backfill_KS1111003_2019.db

tmux new -s ks_498_2021
pixi run -e events backfill-kurosiwo-gfm --year 2021 --events KuroSiwo_498 --db-path backfill_KS498_2021.db

tmux new -s ks_1111011_2020
pixi run -e events backfill-kurosiwo-gfm --year 2020 --events KuroSiwo_1111011 --db-path backfill_KS1111011_2020.db
```

These runs are small (15–39 days each) and are a good way to validate the
workflow end to end. The §5.3 caveats apply: each run uses its own
`--db-path` tracker, and the shared task cache must exist before the first
filtered run (or pass a distinct `--tasks` path per event).

### 5.6 Bookmarking the example events

The static bookmark registry ([`src/atlantis/bookmarks.py`](../../src/atlantis/bookmarks.py),
`python -m atlantis.cli bookmarks add/list/show/remove`) stores named
bbox/date-range shortcuts in `s3://atlantis/assets/bookmarks.parquet`
(override with `ATLANTIS_BOOKMARKS_ROOT` for a local registry) so
`atlantis fetch --event NAME` resolves `--bbox`/`--start-date`/`--end-date`
without retyping them. It is independent of the backfill scripts — the
backfills always read the AOI CSVs — and distinct from the data-driven
`atlantis_events` bookmarks written inside the Zarr archive.

Register the six example events with their AOI bboxes and metadata windows
(the build scripts add the 14-day pad themselves):

```bash
# GEOID-Flood
PYTHONPATH=src pixi run python -m atlantis.cli bookmarks add EMSR864-21 \
    --bbox "-8.9795 39.6862 -8.7403 39.7787" \
    --start-date 2026-03-01 --end-date 2026-03-15 --source gfm \
    --label "GEOID-Flood smallest-event example"

PYTHONPATH=src pixi run python -m atlantis.cli bookmarks add EMSR184-4 \
    --bbox "144.804 -33.9205 146.2484 -32.9803" \
    --start-date 2016-09-23 --end-date 2016-10-09 --source gfm \
    --label "GEOID-Flood smallest-event example"

PYTHONPATH=src pixi run python -m atlantis.cli bookmarks add EMSR292-1 \
    --bbox "24.459 40.7659 25.0766 41.1463" \
    --start-date 2018-06-25 --end-date 2018-07-13 --source gfm \
    --label "GEOID-Flood smallest-event example"

# KuroSiwo
PYTHONPATH=src pixi run python -m atlantis.cli bookmarks add KuroSiwo_1111003 \
    --bbox "43.0908 11.4904 43.2115 11.6087" \
    --start-date 2019-11-09 --end-date 2019-12-11 --source gfm \
    --label "KuroSiwo smallest-event example"

PYTHONPATH=src pixi run python -m atlantis.cli bookmarks add KuroSiwo_498 \
    --bbox "0.1483 43.5262 3.6294 45.4354" \
    --start-date 2021-01-15 --end-date 2021-02-16 --source gfm \
    --label "KuroSiwo smallest-event example"

PYTHONPATH=src pixi run python -m atlantis.cli bookmarks add KuroSiwo_1111011 \
    --bbox "121.1434 16.7234 122.2502 18.4498" \
    --start-date 2020-10-20 --end-date 2020-11-27 --source gfm \
    --label "KuroSiwo smallest-event example"
```

Check the registry with `… bookmarks list`, inspect one entry with
`… bookmarks show EMSR184-4`, and update or delete entries with
`… bookmarks add … --force` / `… bookmarks remove NAME`. The registry is
shared (S3), so bookmarks are visible to every user of the same object
store; the backfilled GFM data itself remains the source of truth in the
yearly cubes.

> **Bookmarks do not load data back from the Zarr.** The static registry
> only feeds `fetch --event` (raw granule fetching). The archive reader's
> `read(..., event=…)` resolves a _different_, data-driven registry
> (`atlantis_events`, written inside the cube by
> `ArchiveWriter.write(..., event=…)`), and the backfill scripts never write
> it. Read a backfilled event back by bbox + dates instead — see §5.7.

### 5.7 Loading back a backfilled event

After a §5.5 backfill, load the archived GFM data with the archive reader,
using the event's AOI bbox (same values as the bookmarks in §5.6) and the
window including the 14-day pad (use the "Pad end" from §5.5):

```python
from atlantis.archive.reader import ArchiveReader

# EMSR184-4 was backfilled into the 2016 yearly cube
reader = ArchiveReader("s3://atlantis/zarr/2016")
ds = reader.read(
    "gfm",
    bbox=(144.804, -33.9205, 146.2484, -32.9803),
    start="2016-09-23",
    end="2016-10-23",  # pad end — the backfill's full window
)

# gfm group channels: water_fraction, exclusion_mask, reference_water
print(ds.water_fraction)   # lazy, CF-decoded (float [0,1], NaN = NODATA)
```

Notes:

- `reader.read(..., event="EMSR184-4")` does **not** work for backfilled
  events — the backfill never records `atlantis_events` in the cube, so
  `list_events()` stays empty for these runs. Always select by
  bbox/`start`/`end` (or a full-year read).
- The same selection is available interactively without code:
  `PYTHONPATH=src pixi run python -m atlantis.cli viz serve gfm --stac <catalog>
--bbox "…" --start … --end …` after building a STAC catalog (see
  [stac-and-viz.md](./stac-and-viz.md)).
- For a multi-year event, read each year's cube separately and concatenate,
  mirroring the split backfill runs (§5.3).

---

## 6. Mechanics worth knowing

- **Windows.** Each (event, AoI) is processed over its metadata date range
  plus a 14-day post-flood pad (`DEFAULT_POST_FLOOD_PAD_DAYS` in
  `event_tasks.py`).
- **Catalogue years are built offline**, from
  `s3://atlantis/assets/gfm/gfm_archive_catalog_{year}.parquet` (2021–2025).
  Days outside those years are searched live on the EODC STAC API day by day;
  days without GFM items are skipped (including the expected empty pre-2021
  searches for GEOID-Flood).
- **Dropped items.** Items whose STAC metadata lacks a valid `Equi7Tile` or
  bbox cannot be placed on a tile task; they are recorded to
  `<tasks>.dropped.json` (alongside the task-list cache) rather than lost.
- **Stale task-list detection.** A cached task list in the old
  512-arcmin-block format is detected via the EQUI7 tile-id format check and
  rebuilt automatically; if a tracker was created by such a run, delete it so
  the new task ids are not skipped as `DONE`.
- **Post-run validation.** `validate_cube` asserts every task date is present
  on the gfm `time` axis and the axis is strictly ascending, and prints the
  axis growth (`axis_before → axis_after`, `366 = full year`).
- **Scale.** The batch engine settings (workers, per-worker memory) match the
  production GFM cube build — see the
  [GFM settings section](./cube-build.md#gfm-settings-recommended-values-and-change-risks)
  for capacity guidance.
