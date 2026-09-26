# Incremental VIIRS Archive Updates — Operational Guide

> Weekly, resume-safe ingestion of newly published VIIRS NOAA S3 AOI tiles
> into the yearly Atlantis Zarr archive. The same flow as the MODIS update
> ([modis-archive-update.md](./modis-archive-update.md)): reconcile the
> expected task inventory against the SQLite tracker and the archive, process
> only the missing/failed work, keep the time axis strictly ascending, and
> record every run in an immutable manifest with an S3-backed copy of the
> state.

**Source of truth**

| Concern                              | Module                                                                                   |
| ------------------------------------ | ---------------------------------------------------------------------------------------- |
| Orchestration (worker, reconcile, …) | [`src/atlantis/archive/update.py`](../../src/atlantis/archive/update.py)                 |
| Ascending-order writer wrapper       | [`src/atlantis/archive/ordering.py`](../../src/atlantis/archive/ordering.py)             |
| Offline time-axis reindex migration  | [`src/atlantis/archive/reindex_time.py`](../../src/atlantis/archive/reindex_time.py)     |
| Task requeue helper                  | [`src/atlantis/batch/tracker.py`](../../src/atlantis/batch/tracker.py)                   |
| Underlying cube engine               | [`src/atlantis/archive/cube_batch.py`](../../src/atlantis/archive/cube_batch.py)         |
| VIIRS catalogue builder + AOI grid   | [`src/atlantis/fetchers/viirs/catalog.py`](../../src/atlantis/fetchers/viirs/catalog.py) |
| Tests                                | [`tests/archive/test_update.py`](../../tests/archive/test_update.py)                     |

---

## 1. What this feature does

The yearly archive is one cube per calendar year:

```text
s3://atlantis/zarr/
├── 2025/datacube.zarr/{gfm,modis,viirs,zarr.json}
├── 2026/datacube.zarr/{gfm,modis,viirs,zarr.json}
└── ...
```

`atlantis archive viirs update` keeps a year's `viirs` group complete and
current:

1. **Refreshes the year's catalogue** — lists the NOAA S3 bucket for the
   update window, merges the result into `viirs_archive_catalog_<year>.parquet`
   (candidate-then-promote), so newly published tiles are always discovered —
   even when the tracker shows nothing pending.
2. **Reconciles** every expected task ID against the tracker and the archive:
   `DONE` tasks that are missing from the archive are requeued after a
   warning; archive dates with no catalogue coverage are reported as orphans
   (never deleted).
3. **Processes only unresolved work** through the existing resume-safe cube
   batch engine, wrapped in an ascending-order writer so the time axis never
   grows out of chronological order.
4. **Validates** (all expected tasks `DONE`, dates present on the axis, axis
   strictly ascending), advances a **contiguous watermark**, and writes an
   **immutable run manifest**.
5. **Backs up** tracker, manifest, and catalogue to
   `s3://atlantis/archive-state/viirs/` in a `finally` path — also on failure.

MODIS and GFM are untouched: only the `viirs` group and its metadata change.

**Differences from the MODIS flow**

| Aspect           | MODIS                                                                    | VIIRS                                                                                     |
| ---------------- | ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| Source inventory | LAADS (MCDWD h/v tiles)                                                  | NOAA public S3 (`noaa-jpss.s3.amazonaws.com`), AOI tiles                                  |
| Tiles            | `modis-YYYYMMDD-hHHvVV` per `(date, h, v)`                               | one granule per `(date, aoi_id)`; `task_id` is the filename                               |
| Auth             | `EARTHDATA_TOKEN` (LAADS application token) + preflight probe            | public bucket — no token, no preflight (per-tile retries suffice)                         |
| Cube variables   | `water_fraction`, `exclusion_mask`, `reference_water`, `recurring_flood` | `water_fraction`, `exclusion_mask`, `reference_water`, `cloud_mask`, `snow_ice`, `shadow` |
| State root       | `/mnt/atlantis-state/modis/`                                             | `/mnt/atlantis-state/viirs/`                                                              |
| Yearly catalogue | `s3://atlantis/assets/modis/modis_archive_catalog_<year>.parquet`        | `s3://atlantis/assets/viirs/viirs_archive_catalog_<year>.parquet`                         |
| Backup base      | `s3://atlantis/archive-state/modis/`                                     | `s3://atlantis/archive-state/viirs/`                                                      |

The task IDs are the NOAA filename stems (e.g.
`VIIRS-Flood-1day-GLB077_v1r0_blend_s202007220000000_e...tif`); each filename
embeds its date, so one task ID is unique per `(date, aoi)`.

## 2. CLI surface

`archive` is a Typer sub-application:

```text
atlantis archive viirs update ...    # launch the incremental update (detached tmux by default)
atlantis archive viirs status ...    # inspect yearly tracker / catalogue / archive state
atlantis archive viirs _run-update   # internal foreground worker (spawned by `update`)
atlantis archive viirs seed-tracker  # build a tracker from the archive (onboarding pre-update years)
atlantis archive viirs _reindex-time # one-off time-axis migration (earlier-hole repair)
```

See [the CLI reference](../cli.md) for the full option tables.

### Detached execution by default

`atlantis archive viirs update` resolves the window, then starts a new detached
tmux session and returns immediately:

```text
tmux attach -t atlantis-viirs-update-2026-<runid>    # watch the worker
atlantis archive viirs status --year 2026            # inspect progress
```

The worker runs from the repository root through the Pixi batch environment and
writes its log to `<state-root>/<year>/logs/<runid>.log` — under the **first
resolved year**, so a December/January rollover run logs under the earlier
year. It never launches `update` recursively; `--foreground` runs the same
worker path in the current terminal (for schedulers/CI/tests). `--attach`
attaches to the new tmux session right after launch and `--session-name`
overrides the generated session name. The launcher fails clearly when tmux is
missing or the session already exists — it never falls back to a background
shell process.

All execution is Pixi-only:

```text
PYTHONPATH=src pixi run -e batch python -m atlantis.cli archive viirs _run-update ...
pixi run -e batch viirs-archive-update                   # foreground, production defaults
pixi run -e batch viirs-archive-update-dry-run           # resolve + report only
pixi run -e batch viirs-archive-seed-tracker -- --year YYYY
```

Any run that launches the batch engine (`update`, `_run-update`, or a cube
build) **must** use the `batch` environment — the default environment lacks
`distributed`, so `pixi run viirs-archive-update` without `-e batch` fails at
the Dask import.

## Quick start — run it right now

Prerequisites: AWS credentials for `s3://atlantis` (NOAA S3 itself is public —
no token is needed), pixi, and tmux (detached mode only).

1. Dry run — resolve and print the plan without launching the worker:
   `PYTHONPATH=src pixi run -e batch python -m atlantis.cli archive viirs update --foreground --dry-run`
2. Foreground run (production defaults, current terminal):
   `pixi run -e batch viirs-archive-update`
3. Detached tmux (the CLI default; returns immediately):
   `PYTHONPATH=src pixi run -e batch python -m atlantis.cli archive viirs update --year 2026`
   `tmux attach -t atlantis-viirs-update-2026-<runid>`
4. Inspect progress / results:
   `pixi run -e batch viirs-archive-status -- --year 2026`

What a default run does:

1. Resolves year 2026 (no `--year` defaults to the current year). The window
   is anchored at the archive's **last processed date**, not at today: with
   no local 2026 tracker the run is a catch-up from `2026-01-01` toward
   `today - 7 d lag`, and the 31-day guardrail chunks it forward — the first
   run processes the **next** 31 days (`2026-01-01 → 2026-01-31`, with a
   printed notice). Re-running the same command continues automatically with
   the next 31 days until the year is caught up. Near real time (caught up),
   the window instead re-scans the trailing lookback days up to the latest
   available data.
2. Creates the year state under the state root (e.g.
   `/mnt/atlantis-state/viirs/2026/`) and an **empty** `cube_tracker.db` —
   the tracker from an earlier run exists only in the S3 backup and is never
   restored automatically, so every task in the window is treated as pending
   and re-ingested (idempotent overwrites).
3. Refreshes `viirs_archive_catalog_2026.parquet` from NOAA S3 for the window
   (candidate-then-promote) and selects the window's tasks.
4. Runs the ordered batch; on the year's first build the 2026 axis is
   pre-filled (365 slots), so every date lands in a pre-existing slot and the
   axis stays ascending by construction.
5. Validates (all `DONE`, axis ascending), advances the watermark to the
   chunk's end, writes the immutable manifest, and backs up
   tracker/manifest/catalogue to `s3://atlantis/archive-state/viirs/2026/`.

See §4 for the full per-year pipeline and §7 for the deployment phases.

## 3. Persistent state (per year)

```text
/mnt/atlantis-state/viirs/
├── 2025/
│   ├── cube_tracker.db        # live SQLite task tracker (the task-level source of truth)
│   ├── update.lock            # pid + timestamp; one writer per year
│   ├── catalogues/viirs-2025.parquet
│   ├── manifests/<runid>.json # immutable per-run manifest
│   └── logs/<runid>.log
└── 2026/ ...
```

After every run (success **or** failure) the tracker, manifest, and catalogue
are mirrored to `<backup-base>/<year>/`
(`s3://atlantis/archive-state/viirs/<year>/` by default). The mounted local
tracker is the live database during a run — SQLite is never used directly on
S3.

## 4. How a run works

For each resolved year, in chronological order, under the year lock — the
pipeline is identical to the MODIS flow (see
[modis-archive-update.md](./modis-archive-update.md) §4 for the full stage
walkthrough):

1. **Window resolution** — explicit `--start/--end` for repair/backfill;
   otherwise the window is anchored at the archive's last processed date. The
   catch-up guardrail chunks auto-resolved windows to the next 31 days from
   the watermark; near real time the window re-scans the lookback span. A
   fresh current year also reaches back into the previous year.
2. **Catalogue refresh** — build the fresh NOAA S3 range locally, derive the
   task columns, merge with the existing yearly catalogue, dedupe on
   `(date, aoi_id)` (the freshest row wins), drop rows outside the year,
   validate schema/coverage, write a local candidate, then promote it to the
   canonical per-year object in a single replacement. A failed build leaves
   the previous catalogue intact and stops the run before any cube work.
3. **Task selection** — convert the window's catalogue rows with
   `to_tasks()`. With `--no-retry-failed`, previously `FAILED` tasks are left
   unretried (they still block the watermark).
4. **Append-only hole check** — a date with expected tasks that is missing
   from the archive axis _below_ the axis tail is a repair condition: the run
   refuses to append it out of order and points to `_reindex-time`.
5. **Reconciliation** — classify every expected task as `DONE` / `FAILED` /
   absent; requeue (delete the row of) tasks whose date is `DONE` in the
   tracker but missing from the archive; report orphans. The engine itself
   skips `DONE` and retries `FAILED`, so only genuinely unresolved tiles are
   submitted.
6. **Ordered batch** — the cube engine streams completed tiles through
   `OrderedConsume`, which buffers payloads and only writes a date once every
   earlier date in the window is fully resolved. New time slots are therefore
   always appended in ascending order regardless of Dask completion order. On
   a **new year** the writer session pre-fills the `time` axis with the full
   year before the first write (marker `atlantis_time_prefill`).
7. **Validation** — every expected task `DONE` and none `FAILED`; expected
   dates present on the axis; axis strictly ascending; a sample of DONE tile
   windows checked for all-NODATA (warning only).
8. **Watermark + manifest** — `last_complete` advances only through the
   highest contiguous fully-`DONE` date range from the window start. The
   manifest records `"source": "viirs"`, the window, catalogue checksum,
   tracker path, Dask settings, task totals, watermark, and pipeline revision.
9. **Backup** — tracker, manifest, and catalogue copied to the backup root in
   a `finally` block; a failed backup fails a successful run.

A failed run (validation, stale-lock conflict, catalogue inconsistency, backup
failure) exits non-zero and writes a `status: "failed"` manifest.

**Expected outcomes** — a successful run exits 0 with `Update ok for year(s)
[...]`, every expected task `DONE`, the watermark advanced to the highest
contiguous complete date, a `status: "ok"` manifest under `manifests/`, and
the tracker/manifest/catalogue mirrored to the backup root. A run that
resolves an empty window is a no-op: exit 0, `Resolved window is empty —
nothing to do.`, no manifest. Anything else is a failed run.

## 5. Time-axis ordering policy

Same append-only ascending policy as MODIS. When an earlier hole must be
filled, run the one-off migration first:

```text
atlantis archive viirs _reindex-time --year 2026
```

It rewrites the year's `viirs` group into strictly ascending order, inserting
empty NODATA slots for catalogue dates missing from the axis, then swaps the
group into place. The next `update` run fills those slots in order.

## 6. Failure handling and inspection

- `atlantis archive viirs status --year YYYY` reports: expected / `DONE` /
  `FAILED` counts, watermark (highest contiguous complete date), first/last
  archive date, missing date ranges, time-axis sortedness, the most recent
  failed task IDs with error messages, the last manifest, and lock state —
  plus a per-date completion heatmap and a state-detail section.
- **Prefilled years** (marker `atlantis_time_prefill`): the report sets
  `prefilled_year: true` and computes **missing date ranges from the
  tracker** — dates whose expected tasks are not all `DONE`/`FAILED` —
  instead of `expected − axis`.
- `atlantis archive viirs status` (no `--year`) summarises **all** years with
  local state at once. `pixi run viirs-archive-status` is the shortcut.
- **Downloads:** NOAA S3 is public, so there is no token and no preflight
  probe; transient download failures are covered by the engine's per-tile
  retries.
- Read the tracker directly on the mounted volume:

  ```sql
  SELECT status, COUNT(*) FROM tasks GROUP BY status;
  SELECT task_id, error, attempts, finished_at FROM tasks
  WHERE status = 'FAILED' ORDER BY finished_at DESC;
  ```

- Never delete or recreate a tracker to "fix" a failed run: re-run the same
  year/window under its lock — `DONE` tasks are skipped, `FAILED` tasks are
  retried.
- A lock left by a dead PID (or older than 24 h) is stale and is reclaimed
  automatically; a live lock fails the run.

## 7. Deployment phases

1. **Phase 1 — onboard pre-update years without a tracker:** years archived
   before the update flow existed get their tracker built from the archive
   with `atlantis archive viirs seed-tracker --year YYYY`: every catalogue
   task whose date is on the time axis is marked `DONE`, and catalogue dates
   missing from the axis stay pending and are reported. The next `update` run
   then only processes genuinely missing work.
   **`seed-tracker` refuses a prefilled year** (marker
   `atlantis_time_prefill`): on a full-year axis, "date on axis" proves
   nothing. For a prefilled year, re-run the (resume-safe) cube build to
   rebuild a lost tracker, or use the tracker from the original build.
2. **Phase 2 — initial catch-up for the current year:** the first run builds
   and publishes `viirs_archive_catalog_<year>.parquet` for its window and
   ingests in ascending order, pre-filling the year's time axis and
   establishing the tracker baseline. If the backlog exceeds a month, the
   catch-up guardrail chunks the run to the **next** 31 days from the
   watermark — re-running the same command continues automatically.
3. **Phase 3 — weekly runs:** start the VM and invoke
   `atlantis archive viirs update`. Near real time the effective window is
   `last_complete + 1 - lookback` → `today - lag`; the lookback covers late
   NOAA publications and failed prior runs. At year rollover the job finishes
   outstanding December tasks in the old year before starting January tasks
   in the new one.

## 8. Archive invariants

1. **One writer per VIIRS year** — the per-year lock serialises all updates.
2. **The tracker is the task-level source of truth**, not the latest Zarr time
   coordinate.
3. **The archive and tracker must agree** — a `DONE` task is trusted only when
   its date is present on the archive axis; a mismatch is a repair condition,
   never a reason to advance the watermark.
4. **SQLite stays on a local POSIX filesystem** — the tracker lives on the
   mounted state volume and is backed up to S3 after each run.
5. **Tracker lineage is stable** — a year's tracker is reused only with its
   canonical yearly catalogue; the manifest records the catalogue checksum.
6. **A year is complete only when every expected task is `DONE`** with no
   unresolved `FAILED` tasks.

## 9. Out of scope

- GFM updates and a generic multi-source scheduler.
- Concurrent VIIRS writers for the same archive year.
- Automatic deletion of archive dates, tracker rows, or orphaned data.
- Maintaining the full-history `viirs_archive_catalog.parquet` (a future
  end-of-year process may derive it from the frozen yearly catalogues).
- ETag/fingerprint-based reprocessing of sources already marked `DONE`.
