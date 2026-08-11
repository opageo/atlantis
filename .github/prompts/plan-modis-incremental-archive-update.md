# Plan: Incremental MODIS Archive Updates

## Goal

Automate **MODIS-only** ingestion into the yearly Atlantis Zarr archive, initially
with a scheduled weekly VM run.

The archive layout is one cube per calendar year:

```text
s3://atlantis/zarr/
├── 2025/
│   └── datacube.zarr/
│       └── modis/
├── 2026/
│   └── datacube.zarr/
│       └── modis/
└── ...
```

`ArchiveWriter` already creates `datacube.zarr` beneath the supplied archive
root, so the update workflow must pass `s3://atlantis/zarr/<year>` as
`--archive`. Do not create a separate store per run, month, or source.

### Confirmed current S3 layout

As of 2026-07-29, `s3://atlantis/zarr/2025/datacube.zarr/` is an existing,
shared Zarr v3 store, not a MODIS-only store:

```text
s3://atlantis/zarr/2025/datacube.zarr/
├── gfm/
├── modis/
├── viirs/
└── zarr.json
```

The MODIS updater must preserve the sibling `gfm` and `viirs` groups and
modify only `datacube.zarr/modis` plus root/group metadata written by the
existing Zarr writer. It must never recreate, delete, or copy the whole 2025
store merely to update MODIS.

The currently published MODIS catalogue objects are:

```text
s3://atlantis/assets/modis/modis_archive_catalog.parquet       # existing historical combined catalogue
s3://atlantis/assets/modis/modis_archive_catalog_2024.parquet
s3://atlantis/assets/modis/modis_archive_catalog_2025.parquet
```

Use **yearly catalogues only** for this feature. `modis_archive_catalog_2025.parquet`
is the 2025 reconciliation input, and the updater will create and extend
`modis_archive_catalog_2026.parquet` weekly. The existing combined catalogue
is neither an input nor an output of the incremental updater. Maintaining or
rebuilding a combined full-history catalogue after a year closes is explicitly
out of scope.

Before enabling an update, verify the 2025 per-year catalogue's actual minimum
and maximum dates rather than assuming it covers every 2025 day.

The first scheduled work is deliberately staged:

1. Reconcile and fill every missing MODIS task in **2025**.
2. In a separate, larger run, ingest **2026-01-01 through the current safe
   availability cutoff**.
3. Thereafter run a small weekly update for newly released data only.

This plan does not include VIIRS or GFM.

---

## Execution environment: Pixi only

All implementation, tests, local runs, and tmux workers must execute from the
repository root using **Pixi**. `pixi.toml` is the authoritative dependency,
environment, and task definition for this work; Pixi creates and manages the
project-local `.pixi/` environments.

Use the existing batch environment for archive updates:

```text
pixi run -e batch <command>
```

The worker command launched inside tmux must begin from the repository root and
use the same form, for example:

```text
PYTHONPATH=src pixi run -e batch python -m atlantis.cli archive modis _run-update ...
```

Carry `PYTHONPATH=src` when invoking modules or commands that need source-tree
imports. Prefer adding a named task under `[tasks]` in `pixi.toml` (for example
`modis-archive-update`) once the command shape is stable, then have tmux run
that task. This makes the scheduled VM invocation reproducible.

Do **not** use `uv`, `pip`, a manually created virtual environment, or
`pyproject.toml` to resolve or install dependencies for this feature.
`pyproject.toml` is maintained for the separate uv workflow and is out of
scope for archive-update development and operations.

---

## Existing capabilities to reuse

- `atlantis batch modis catalog` builds a date-bounded Parquet inventory from
  LAADS.
- `atlantis batch modis cube run` runs MODIS processing in Dask and streams
  output through a single Zarr writer.
- `ArchiveWriter` creates the `modis` group and appends time slices to the
  Zarr cube.
- `atlantis.batch.tracker` records `DONE` / `FAILED` task IDs in SQLite.
- MODIS task IDs are stable per `(date, h, v)`:
  `modis-YYYYMMDD-hHHvVV`.

The existing 2025 cube is already multi-source and the existing per-year
catalogue makes 2025 reconciliation possible without re-listing all of LAADS
unless coverage validation finds it incomplete. The S3 listing supplied so far
does **not** establish that a compatible 2025 SQLite tracker exists. Locate its
durable copy before treating any historical task as `DONE`; a Zarr date alone
cannot reconstruct completion at `(date, h, v)` granularity.

The implementation should add a thin orchestration command and shared helper
functions around these building blocks rather than creating another processing
pipeline.

---

## Archive invariants

1. **One writer per MODIS year.** Only one update process may write to
   `s3://atlantis/zarr/<year>/datacube.zarr/modis` at a time. The cube writer
   mutates that group's time metadata.
2. **A durable tracker per year.** The SQLite tracker is the task-level source
   of truth for completion, not merely the latest Zarr time coordinate.
3. **The archive and tracker must agree.** A `DONE` task is trustworthy only
   when its expected date is present in the corresponding archive source group.
   A mismatch is a repair condition, never a reason to advance the watermark.
4. **Never move a SQLite database directly on S3.** SQLite needs local POSIX
   filesystem semantics. Store each tracker on a persistent volume mounted by
   the update VM; back it up to S3 after the process exits.
5. **Keep tracker lineage stable.** Reuse a year's tracker only with its
   canonical, append-only **yearly** MODIS catalogue. Record the catalogue
   object version or checksum in a manifest. A changed source interpretation,
   processing configuration, or replacement catalogue requires an explicit
   migration or a new tracker—not silently reusing `DONE` rows.
6. **A year is complete only when every expected task for its selected coverage
   range is `DONE`, with no unresolved `FAILED` tasks.** The largest date in a
   Zarr `time` coordinate cannot prove that all MODIS tiles or intermediate
   dates were processed.

---

## Persistent operational state

Use a persistent VM-attached disk (or equivalent durable filesystem) for the
live state, for example:

```text
/mnt/atlantis-state/modis/
├── 2025/
│   ├── cube_tracker.db
│   ├── update.lock
│   ├── state.json
│   ├── manifests/
│   │   └── 2026-07-29T010203Z.json
│   └── catalogues/
│       └── modis-2025.parquet
└── 2026/
    ├── cube_tracker.db
    ├── update.lock
    ├── state.json
    ├── manifests/
    └── catalogues/
```

Mirror completed tracker snapshots, catalogues, and manifests to:

```text
s3://atlantis/archive-state/modis/<year>/
```

A snapshot is for inspection and disaster recovery; the mounted local tracker
is the live database during a run. Never run two VMs against the same tracker
or the same MODIS year archive.

`state.json` should minimally record:

```json
{
  "source": "modis",
  "year": 2026,
  "archive_root": "s3://atlantis/zarr/2026",
  "catalogue_uri": "s3://atlantis/assets/modis/modis_archive_catalog_2026.parquet",
  "catalogue_checksum": "...",
  "last_successful_end": "2026-07-22",
  "last_run_id": "2026-07-29T010203Z",
  "pipeline_revision": "git SHA"
}
```

Every run must write an immutable manifest containing its requested and
resolved date ranges, catalogue checksum, archive URI, tracker path, Dask
settings, task totals, `DONE` / `FAILED` counts, timestamps, and final status.

---

## CLI and tmux user experience

Expose the workflow through the existing source-first CLI style:

```text
atlantis archive modis update
```

This follows the established `atlantis batch modis ...` command hierarchy more
closely than `atlantis archive update modis`: `archive` is the capability,
`modis` is the source, and `update` is the action. It also leaves room for a
future `atlantis archive viirs update` without turning source names into a
positional argument with separate parsing rules.

`archive` must become a Typer sub-application while preserving the current
event-import behaviour as an explicit command:

```text
atlantis archive event ...       # existing archive-from-harmonised-GeoTIFF flow
atlantis archive modis update    # incremental yearly archive update
atlantis archive modis status    # inspect yearly tracker and manifest state
```

If backward compatibility for the current top-level `atlantis archive --event
...` invocation is required, retain it as a compatibility alias during the
transition and document its deprecation rather than silently breaking scripts.

### Default detached execution

By default, `atlantis archive modis update` must launch the actual update in a
new detached tmux session and return immediately. The foreground worker is an
internal command—not a second public workflow—such as:

```text
atlantis archive modis _run-update ...
```

The launcher must construct the worker command with the same resolved options,
including year/window, state root, archive base, credentials environment,
tracker location, and run ID. It must never launch the public `update` command
again, which would recursively create tmux sessions.

The launcher must resolve the repository root before starting tmux and execute
the worker through the repository's Pixi batch environment. Do not rely on the
caller’s current directory, system Python, or an activated virtual environment.
The tmux command must be equivalent to:

```text
cd <repository-root> && PYTHONPATH=src pixi run -e batch python -m atlantis.cli archive modis _run-update ...
```

Use a deterministic but collision-safe default session name, for example:

```text
atlantis-modis-update-2026-20260729T010203Z
```

Write stdout/stderr to the durable run directory, for example:

```text
/mnt/atlantis-state/modis/2026/logs/20260729T010203Z.log
```

After launch, print exactly the session name, log path, target archive,
tracker path, and attach/status follow-ups. The user can then inspect the work
without needing to reconstruct a long Dask command:

```text
tmux attach -t atlantis-modis-update-2026-20260729T010203Z
atlantis archive modis status --year 2026
```

Suggested options:

```text
--start YYYY-MM-DD              # explicit repair/backfill start
--end YYYY-MM-DD                # explicit inclusive end
--year YYYY                     # restrict to one archive year
--lookback-days 14              # weekly run only; default configurable
--availability-lag-days 7       # avoid querying data still being published
--archive-base s3://atlantis/zarr
--state-root /mnt/atlantis-state/modis
--catalogue-uri s3://atlantis/assets/modis/modis_archive_catalog_<year>.parquet
--workers-min / --workers-max / --memory-limit
--dry-run
--retry-failed
--session-name NAME             # override the generated tmux session name
--attach                        # attach after creating the tmux session
--foreground                    # run in this terminal; no tmux, for CI/schedulers/tests
```

The worker must print, before doing work:

- each year and date range selected;
- the target archive (`s3://atlantis/zarr/<year>`);
- tracker database path;
- expected, `DONE`, missing, and previously `FAILED` task counts;
- whether it is a reconciliation, catch-up, or weekly update.

A successful exit is allowed only after all selected tasks are `DONE` and
post-write validation passes. It must return non-zero for unresolved failures,
a stale lock, state/catalogue inconsistency, or validation failure.

The launcher must fail clearly if tmux is unavailable or a requested session
already exists; do not silently fall back to a background shell process. Use
`--foreground` deliberately for non-interactive environments such as tests and
an external scheduler. The launcher itself only confirms tmux started; final
success or failure is the worker's exit code, durable manifest, log, and
`status` output.

---

## Reconciliation and gap detection

### Why task-level reconciliation is necessary

A MODIS date can contain many `(h, v)` tiles. The Zarr `time` array answers
only “does the date have a slot?”, not “were all expected tiles written?”
Likewise, a maximum date does not expose earlier gaps. Reconciliation therefore
compares the expected task IDs from the catalogue with the tracker.

### Per-year reconciliation algorithm

For every selected year, in chronological order:

1. Load `s3://atlantis/assets/modis/modis_archive_catalog_<year>.parquet`.
   For a new year, build its first date range and publish this file before
   creating tasks. Validate that every selected row has a `date` inside that
   calendar year and requested coverage window before proceeding.
2. Convert the filtered rows with the existing `modis.inventory.to_tasks()`.
3. Read `cube_tracker.db` and classify every expected task ID as:
   - `DONE`;
   - `FAILED`;
   - absent / pending.
4. Open the corresponding archive source group, if it exists, and inspect its
   `time` coordinate.
5. Reconcile tracker and archive:
   - If a date has expected `DONE` tasks but is missing from the archive time
     axis, flag the date as inconsistent and requeue its tasks after an
     operator-visible warning.
   - If the archive contains a date but expected tasks are absent or failed in
     the tracker, treat the missing tasks as a gap; do not call the date
     complete.
   - If an archive date exists with no catalogue coverage, report it as an
     informational orphan rather than deleting data automatically.
6. Select all pending tasks and, by default, all previously `FAILED` tasks.
7. Submit only that selected set to the existing MODIS cube batch runner,
   using the year's archive root and tracker.
8. Re-read the tracker and archive after completion. Advance
   `last_successful_end` only through the highest **contiguous** fully-complete
   date range beginning at the intended start date.

Add a tracker helper that can safely move an inconsistent `DONE` task back to
pending (or delete its task row) under the year lock. Do not ask operators to
edit SQLite rows by hand for normal repair flows.

---

## Time-ordering prerequisite

The current `datacube.ensure_time_index()` appends an unseen date to the end
of the time axis. That is correct for new weekly dates but can leave the time
axis out of chronological order when filling an earlier hole in an existing
year.

Before enabling automatic historical gap repair, choose and implement one of
these policies:

1. **Preferred:** make the archive time coordinate sorted when inserting a
   missing earlier date, moving/reindexing affected time slices safely; or
2. **Restricted v1:** only allow append-only repair after the latest complete
   date. Detect earlier holes, report them, and require a separate explicitly
   designed backfill operation.

Do not silently write an older missing date at the physical end of the time
array while claiming chronological append semantics.

The planned bootstrap order minimises this issue: complete 2025 first, then
start the 2026 cube from January in date order, then append weekly dates in
ascending order.

---

## Weekly yearly-catalogue refresh and publication

The existing `batch modis catalog` command builds a fresh range and overwrites
its output. Every update run must discover latest products by refreshing the
active year's yearly catalogue before it decides which cube tasks to process.
The update workflow needs incremental catalogue support:

1. Determine the catalogue refresh range. For a weekly run, use the same
   lookback/availability window as ingestion so late LAADS publications are
   discovered; for the initial 2026 catch-up, use `2026-01-01` through the
   safe availability cutoff.
2. Build a local catalogue for only that refresh range.
3. Load the current `modis_archive_catalog_<year>.parquet`, if it exists.
   A first run for a new year starts with an empty catalogue.
4. Merge and deduplicate on `(date, h, v, source_uri)`.
5. Sort by `(date, h, v)`, reject rows outside that year, and validate the
   required schema and refreshed date coverage.
6. Write a versioned candidate object and validate it. Publish the canonical
   per-year object in one S3 object replacement only after validation succeeds
   (use object versioning or a conditional write where the object store
   supports it).
7. Use the newly published yearly catalogue as the sole task inventory for
   reconciliation and ingestion. Record its checksum/version in the run
   manifest and `state.json`.

Do not read, rewrite, or extend `modis_archive_catalog.parquet` in this
workflow. A later, separate end-of-year process may construct a combined
historical catalogue from frozen yearly catalogues.

Never overwrite the canonical yearly S3 Parquet catalogue directly from an
interrupted network-bound listing job. A catalogue build failure must leave the
previous yearly inventory intact and stop before any cube work begins.

For the initial 2025 reconciliation, start from the existing
`modis_archive_catalog_2025.parquet`. Use it only after recording its checksum
and validating date coverage. Rebuild and republish 2025 only if validation
shows a coverage/schema problem.

The weekly refresh is required even when the tracker shows no pending tasks:
the catalogue is the mechanism that reveals newly published MODIS tiles and
dates. A no-op cube phase is valid only after the refreshed yearly catalogue
has been reconciled with the tracker.

---

## Execution phases

### Phase 0 — Preflight and one-time setup

1. Confirm `EARTHDATA_TOKEN` is available to the VM through its secret manager.
2. Confirm the VM identity can read/write the Atlantis object store and read
   LAADS.
3. Provision and mount the persistent state volume.
4. Verify the existing `s3://atlantis/zarr/2025/datacube.zarr/zarr.json` and
   `modis/` group open successfully. Create only missing future-year archive
   roots and `s3://atlantis/archive-state/modis/`; never initialise or replace
   the existing 2025 root.
5. From the repository root, validate that `pixi run -e batch` resolves the
   project-local `.pixi/` environment and that the expected HDF4-capable GDAL
   stack is available. Do not use the uv environment or `pyproject.toml` for
   this validation.
6. Define the initial coverage boundary and availability lag with the MODIS
   product owners.

### Phase 1 — Reconcile and complete 2025

Run the orchestration command explicitly for 2025. It must:

1. Validate the existing
   `s3://atlantis/assets/modis/modis_archive_catalog_2025.parquet` and record
   its checksum in the initial manifest.
2. Inspect the existing shared cube's
   `s3://atlantis/zarr/2025/datacube.zarr/modis` group and locate the matching
   durable 2025 tracker without touching the sibling `gfm` / `viirs` groups.
   Do not manufacture `DONE` rows from the Zarr time axis: if the original
   tracker cannot be recovered, classify the 2025 task-level state as unknown
   and run an explicit verification/rebuild workflow before declaring gaps or
   completeness.
3. Identify all missing and failed `(date, h, v)` tasks across the selected
   2025 coverage.
4. Process only those tasks into `s3://atlantis/zarr/2025`.
5. Validate 2025 and publish a successful manifest only when every expected
   task is `DONE`.

This is a potentially large catch-up job. Run it through `atlantis archive
modis update --year 2025`, which starts its own persistent tmux session; do not
overlap it with another 2025 MODIS job.

### Phase 2 — Initial 2026 catch-up

After Phase 1 succeeds, run a separate catch-up from `2026-01-01` through:

```text
current date - availability lag
```

Target only:

```text
s3://atlantis/zarr/2026/datacube.zarr/modis
```

First build and publish
`s3://atlantis/assets/modis/modis_archive_catalog_2026.parquet` for this date
range, then process dates in ascending order. This establishes a chronological
2026 time axis, a yearly catalogue, and a tracker baseline before weekly
automation begins.

### Phase 3 — Weekly incremental runs

Once per week, start the VM and invoke `atlantis archive modis update`. The
command starts the detached tmux worker on that VM. Its effective window is:

```text
start = max(2026-01-01, last_successful_end + 1 day - lookback_days)
end   = today - availability_lag_days
```

First refresh the active year's `modis_archive_catalog_<year>.parquet` over
this window. Then reconcile all task IDs in the refreshed yearly catalogue for
that window, retry failures, and append only task IDs not already `DONE`.

The lookback covers late publication or a failed prior run. Do not permit the
lookback to write an earlier date without satisfying the time-ordering policy
above.

At year rollover, the same job must finish outstanding December tasks in
`.../2026` before starting January tasks in `.../2027`; each year has its own
lock, archive root, tracker, state, and manifest.

---

## Failure handling and tracker inspection

Failures must be easy to diagnose without reopening the Zarr store or replaying
a full job.

For each run, print the tmux session, durable log path, tracker path, and
manifest location. Provide `atlantis archive modis status --year YYYY` that
reports:

- expected task count for the requested window;
- `DONE`, `FAILED`, and pending counts;
- first/last complete date and missing date ranges;
- the most recent failed task IDs and error messages;
- archive/tracker mismatch count;
- last successful run and watermark.

Operators must also be able to inspect the SQLite database directly on the
mounted volume:

```sql
SELECT status, COUNT(*) FROM tasks GROUP BY status;

SELECT task_id, error, attempts, finished_at
FROM tasks
WHERE status = 'FAILED'
ORDER BY finished_at DESC;
```

Document the exact read-only inspection command, the tracker path convention,
and recovery procedure. Do not delete or recreate a tracker to “fix” a failed
run: rerun the same year/window under its lock, which naturally retries
`FAILED` and absent tasks while skipping `DONE`.

Back up the tracker and manifest in a `finally` path, even when a run fails.
The VM should exit non-zero after that backup so the external scheduler alerts.

---

## Validation and success criteria

After each selected year/window completes:

1. Assert that all selected expected task IDs are `DONE` and none are `FAILED`.
2. Open `datacube.zarr` remotely and assert the `modis` group is readable.
3. Assert expected dates are represented on the archive time coordinate.
4. Sample expected tile windows and verify they contain non-NODATA data where
   the product exists.
5. Validate the catalogue schema, sorting, uniqueness, and coverage bounds.
6. Confirm the time coordinate satisfies the declared chronological policy.
7. Write the run manifest and advance the watermark only after all checks pass.

A failed validation leaves the watermark unchanged and must make the next run
reconcile the same window again.

---

## Tests

Add tests for:

1. Year-based archive routing (`2025` → `s3://atlantis/zarr/2025`).
2. Date-window splitting across 31 December / 1 January.
3. Gap detection from expected task IDs versus tracker rows—not only max date.
4. `FAILED` task retry and `DONE` task skipping.
5. Archive/tracker mismatch detection and safe task requeue.
6. Contiguous watermark advancement; a later completed date must not skip an
   earlier failed/missing date.
7. Catalogue merge, deduplication, validation, and atomic promotion ordering.
8. Run lock behaviour and stale-lock recovery.
9. Durable tracker snapshot on successful and failed runs.
10. A local-store integration test covering: partial 2025 state → gap repair →
    complete 2025 → initial 2026 catch-up → weekly append.
11. Launcher behaviour: it creates one correctly named tmux session, propagates
    all resolved worker options, starts at the repository root through
    `PYTHONPATH=src pixi run -e batch`, writes the run log path, and does not
    recurse.
12. `--foreground` runs the same worker path without requiring tmux, while a
    missing tmux executable fails clearly in default detached mode.
13. The update task resolves dependencies only from `pixi.toml` / `.pixi/` and
    does not invoke `uv`, `pip`, or a `pyproject.toml`-defined workflow.

Run targeted tests through Pixi, for example:

```text
pixi run -e batch pytest -q tests/archive tests/batch tests/fetchers/modis
pixi run -e batch ruff check src/atlantis tests
```

Add a reproducible Pixi task for the production-style dry run rather than
maintaining an undocumented shell command.

---

## Out of scope for this increment

- VIIRS, GFM, or a generic multi-source scheduler.
- An archive rewrite or a redesign of the Zarr spatial schema.
- Concurrent MODIS writers for the same archive year.
- Automatic deletion of archive dates, tracker rows, or orphaned data.
- Maintaining the full-history `modis_archive_catalog.parquet`; a future
  end-of-year process may derive it from frozen yearly catalogues.
- Fingerprint-based reprocessing of source files already marked `DONE`.

Source-version/ETag fingerprints and automatic upstream-revision detection are
valuable follow-up work after the append and gap-recovery workflow is proven.
