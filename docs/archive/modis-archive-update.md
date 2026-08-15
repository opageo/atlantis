# Incremental MODIS Archive Updates — Operational Guide

> Weekly, resume-safe ingestion of newly published MODIS MCDWD tiles into the
> yearly Atlantis Zarr archive. Reconciles the expected task inventory against
> the SQLite tracker and the archive, processes only the missing/failed work,
> keeps the time axis strictly ascending, and records every run in an
> immutable manifest with an S3-backed copy of the state.

**Source of truth**

| Concern                              | Module                                                                               |
| ------------------------------------ | ------------------------------------------------------------------------------------ |
| Orchestration (worker, reconcile, …) | [`src/atlantis/archive/update.py`](../../src/atlantis/archive/update.py)             |
| Ascending-order writer wrapper       | [`src/atlantis/archive/ordering.py`](../../src/atlantis/archive/ordering.py)         |
| Offline time-axis reindex migration  | [`src/atlantis/archive/reindex_time.py`](../../src/atlantis/archive/reindex_time.py) |
| Task requeue helper                  | [`src/atlantis/batch/tracker.py`](../../src/atlantis/batch/tracker.py)               |
| Underlying cube engine               | [`src/atlantis/archive/cube_batch.py`](../../src/atlantis/archive/cube_batch.py)     |
| Tests                                | [`tests/archive/test_update.py`](../../tests/archive/test_update.py)                 |

---

## 1. What this feature does

The yearly archive is one cube per calendar year:

```text
s3://atlantis/zarr/
├── 2025/datacube.zarr/{gfm,modis,viirs,zarr.json}
├── 2026/datacube.zarr/{gfm,modis,viirs,zarr.json}
└── ...
```

`atlantis archive modis update` keeps a year's `modis` group complete and
current:

1. **Refreshes the year's catalogue** — lists LAADS for the update window,
   merges the result into `modis_archive_catalog_<year>.parquet`
   (candidate-then-promote), so newly published tiles are always discovered —
   even when the tracker shows nothing pending.
2. **Reconciles** every expected task ID (`modis-YYYYMMDD-hHHvVV`) against the
   tracker and the archive: `DONE` tasks that are missing from the archive are
   requeued after a warning; archive dates with no catalogue coverage are
   reported as orphans (never deleted).
3. **Processes only unresolved work** through the existing resume-safe cube
   batch engine, wrapped in an ascending-order writer so the time axis never
   grows out of chronological order.
4. **Validates** (all expected tasks `DONE`, dates present on the axis, axis
   strictly ascending), advances a **contiguous watermark**, and writes an
   **immutable run manifest**.
5. **Backs up** tracker, manifest, and catalogue to `s3://atlantis/archive-state/`
   in a `finally` path — also on failure.

VIIRS and GFM are untouched: only the `modis` group and its metadata change.

## 2. CLI surface

`archive` is a Typer sub-application:

```text
atlantis archive event ...            # write harmonised event GeoTIFFs into the cube
atlantis archive modis update ...     # launch the incremental update (detached tmux by default)
atlantis archive modis status ...     # inspect yearly tracker / catalogue / archive state
atlantis archive modis _run-update    # internal foreground worker (spawned by `update`)
atlantis archive modis seed-tracker   # build a tracker from the archive (onboarding pre-update years)
atlantis archive modis _reindex-time  # one-off time-axis migration (earlier-hole repair)
```

See [the CLI reference](../cli.md) for the full option tables.

### Detached execution by default

`atlantis archive modis update` resolves the window, then starts a new detached
tmux session and returns immediately:

```text
tmux attach -t atlantis-modis-update-2026-<runid>     # watch the worker
atlantis archive modis status --year 2026             # inspect progress
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
PYTHONPATH=src pixi run -e batch python -m atlantis.cli archive modis _run-update ...
pixi run -e batch modis-archive-update                   # foreground, production defaults
pixi run -e batch modis-archive-update-dry-run           # resolve + report only
pixi run -e batch modis-archive-seed-tracker -- --year YYYY
```

## Quick start — run it right now

Prerequisites: `EARTHDATA_TOKEN` (a LAADS application token, not an Earthdata
Login token — see §6), AWS credentials for `s3://atlantis`, pixi, and tmux
(detached mode only). The token is loaded from the repo `.env` by the CLI.

1. Dry run — resolve and print the plan without launching the worker:
   `PYTHONPATH=src pixi run -e batch python -m atlantis.cli archive modis update --foreground --dry-run`
2. Foreground run (production defaults, current terminal):
   `pixi run -e batch modis-archive-update`
3. Detached tmux (the CLI default; returns immediately):
   `PYTHONPATH=src pixi run -e batch python -m atlantis.cli archive modis update --year 2026`
   `tmux attach -t atlantis-modis-update-2026-<runid>`
4. Inspect progress / results:
   `pixi run -e batch modis-archive-status -- --year 2026`

What a default run does with the current archive (as of 2026-08-14):

1. Resolves year 2026 (no `--year` defaults to the current year). The window
   is anchored at the archive's **last processed date**, not at today: with
   no local 2026 tracker the run is a catch-up from `2026-01-01` toward
   `today - 7 d lag`, and the 31-day guardrail chunks it forward — the first
   run processes the **next** 31 days (`2026-01-01 → 2026-01-31`, with a
   printed notice). Re-running the same command continues automatically with
   the next 31 days (`2026-02-01 → …`) until the year is caught up; no
   explicit dates needed. Near real time (caught up), the window instead
   re-scans the trailing lookback days up to the latest available data.
2. Creates the year state under the state root (e.g.
   `/mnt/atlantis-state/modis/2026/`) and an **empty** `cube_tracker.db` —
   the tracker from an earlier run exists only in the S3 backup and is never
   restored automatically, so every task in the window is treated as pending
   and re-ingested (idempotent overwrites, ~8,800 tiles per 31-day chunk,
   ~6–7 h at the default 2–6 Dask workers).
3. Refreshes `modis_archive_catalog_2026.parquet` from LAADS for the window
   (candidate-then-promote) and selects the window's tasks.
4. Preflights one real tile download — a missing/invalid `EARTHDATA_TOKEN`
   aborts the run fast (never reaches Dask).
5. Runs the ordered batch; on the year's first build the 2026 axis is
   pre-filled (365 slots), so every date lands in a pre-existing slot and the
   axis stays ascending by construction.
6. Validates (all `DONE`, axis ascending), advances the watermark to the
   chunk's end (`2026-01-31`), writes the immutable manifest, and backs up
   tracker/manifest/catalogue to `s3://atlantis/archive-state/modis/2026/`.

See §4 for the full per-year pipeline and §7 for the deployment phases.

## 3. Persistent state (per year)

```text
/mnt/atlantis-state/modis/
├── 2025/
│   ├── cube_tracker.db        # live SQLite task tracker (the task-level source of truth)
│   ├── update.lock            # pid + timestamp; one writer per year
│   ├── catalogues/modis-2025.parquet
│   ├── manifests/<runid>.json # immutable per-run manifest
│   └── logs/<runid>.log
└── 2026/ ...
```

After every run (success **or** failure) the tracker, manifest, and catalogue
are mirrored to `<backup-base>/<year>/`
(`s3://atlantis/archive-state/modis/<year>/` by default). The mounted local
tracker is the live database during a run — SQLite is never used directly on
S3.

## 4. How a run works

For each resolved year, in chronological order, under the year lock:

1. **Window resolution** — explicit `--start/--end` for repair/backfill;
   otherwise the window is anchored at the archive's **last processed date**,
   not at today: the cursor is the day after the year's contiguous watermark
   (January 1 on a wiped/fresh year) and the end is the latest available
   data (`today - availability lag`, clamped to the year end). Windows
   spanning 31 December split across both years; each year runs under its own
   lock, tracker, and manifest.

   **Catch-up guardrail** — when the year is more than 31 days behind the
   latest available data, an auto-resolved window processes the **next 31
   days from the cursor** and prints a notice: re-running the same command
   continues automatically with the next 31 days, so a long backlog is
   ingested in order across successive resume-safe runs — no explicit dates
   needed. Near real time (caught up, less than a chunk behind), the window
   instead covers `cursor - lookback → today - lag` (weekly defaults: 14 /
   7 days) so late LAADS publications are re-scanned and a complete year is
   still validated and re-recorded. Explicit windows are resolved verbatim
   and never chunked — repair example:
   `… archive modis update --year 2026 --start 2026-06-17 --end 2026-07-22 --foreground`.

   When the current year has no tracker baseline yet (fresh year), the window
   also reaches back into the previous year, so a December backlog and the
   new year's first days are both covered automatically: each year runs under
   its own lock, tracker, and manifest, and a new year is prepared on the
   fly — its catalogue built, its `time` axis pre-filled (marker
   `atlantis_time_prefill`), and its dates ingested.

2. **Catalogue refresh** — build the fresh LAADS range locally, merge with the
   existing yearly catalogue, dedupe on `(date, h, v)` (the freshest row wins),
   drop rows outside the year, validate schema/coverage, write a local
   candidate, then promote it to the canonical per-year object in a single
   replacement. A failed build leaves the previous catalogue intact and stops
   the run before any cube work.
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
   always appended in ascending order regardless of Dask completion order.
   On a **new year** (no archive group yet) the writer session pre-fills the
   `time` axis with the full year (365/366 slots, marker
   `atlantis_time_prefill`) before the first write, so every date lands in a
   pre-existing slot — backfills can never disturb the axis, and the
   append-only hole check (step 4) is inert on prefilled years. Years whose
   axis already exists are untouched: a full axis is a no-op and a
   partially-built one is skipped by the prefill data guard.
7. **Validation** — every expected task `DONE` and none `FAILED`; expected
   dates present on the axis; axis strictly ascending; a sample of DONE tile
   windows checked for all-NODATA (warning only).
8. **Watermark + manifest** — `last_complete` advances only through the
   highest contiguous fully-`DONE` date range from the window start; a later
   completed date never skips an earlier gap. The manifest records the window,
   catalogue checksum, tracker path, Dask settings, task totals, watermark,
   and pipeline revision.
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

The cube engine consumes `as_completed`, so a naive writer would append unseen
dates in completion order — not chronological order. The update worker enforces
**append-only ascending**: new dates are always written after every earlier
date in the window, and an older missing date is never appended at the physical
end of the axis.

When an earlier hole must be filled (for example a date whose tiles all failed
in a previous run, or disorder inherited from an older build), run the one-off
migration first:

```text
atlantis archive modis _reindex-time --year 2026
```

It rewrites the year's `modis` group into strictly ascending order, inserting
empty NODATA slots for catalogue dates missing from the axis, then swaps the
group into place. The next `update` run fills those slots in order. On a remote
store this copies the group's materialised data once — it is a deliberate
one-off, not part of the weekly path.

## 6. Failure handling and inspection

- `atlantis archive modis status --year YYYY` reports: expected / `DONE` /
  `FAILED` counts, watermark (highest contiguous complete date), first/last
  archive date, missing date ranges, time-axis sortedness, the most recent
  failed task IDs with error messages, the last manifest, and lock state —
  plus a per-date completion heatmap (one row per month, one cell per day:
  `#` complete / `x` failed / `o` pending / `.` no data; ASCII glyphs keep it
  readable in any terminal) and a state-detail section with day
  counts and up to eight contiguous ranges per state (`… (+N more)` beyond
  that), alongside expected / incomplete / failed task totals.
- **Prefilled years** (marker `atlantis_time_prefill`, written by
  `batch modis cube run` on a `zarr/<YYYY>` root or — since the update flow
  pre-fills by default — by the update itself on a new year's first run; see
  [cube-build.md](./cube-build.md) → "Pre-filled time axis for year builds"):
  the report sets `prefilled_year: true` and computes **missing date ranges
  from the tracker** — dates whose expected tasks are not all `DONE`/`FAILED`
  — instead of `expected − axis` (the axis always contains every date, so the
  axis-based computation would be empty by construction).
- `atlantis archive modis status` (no `--year`) summarises **all** years with
  local state at once: one row per year (counts, watermark, axis sortedness,
  last run status) plus a one-line monthly overview strip (one block per
  month, dominant state). `pixi run modis-archive-status` is the shortcut.
- **LAADS download auth:** `EARTHDATA_TOKEN` must be a **LAADS application
  token** (create at
  `https://ladsweb.modaps.eosdis.nasa.gov/profiles/#app-tokens`), not an
  Earthdata Login access token — directory listings authenticate either way,
  but file downloads reject the latter; the one-time LAADS DAAC data-archive
  license must also be accepted in a browser (visit any file URL logged in).
  A wrong token or unaccepted license fails every tile download — the update
  preflights one tile and aborts fast with an actionable message. Transient
  preflight failures (connection/timeout, 404/5xx) are retried and then
  warn-and-continue; a persistent `401`/`403` (or an HTML/empty-body auth
  signal) aborts the run — it is not absorbed by per-tile retries.
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

The first scheduled work is deliberately staged:

1. **Phase 1 — reconcile 2025:** `atlantis archive modis update --year 2025`.
   Validates the existing 2025 yearly catalogue, locates the durable 2025
   tracker, and fills every missing/failed task. If the original 2025 tracker
   cannot be recovered, build one from the archive with `seed-tracker` first
   (see below).
2. **Phase 1b — onboard pre-update years without a tracker:** years archived
   before the update flow existed (2024, 2025, …) get their tracker built from
   the archive with `atlantis archive modis seed-tracker --year YYYY`: every
   catalogue task whose date is on the time axis is marked `DONE` (a date on
   the axis proves it was written; the mosaic cannot be decomposed per tile),
   and catalogue dates missing from the axis stay pending and are reported.
   The next `update` run then only processes genuinely missing work.
   **`seed-tracker` refuses a prefilled year** (marker
   `atlantis_time_prefill`): on a full-year axis, "date on axis" proves
   nothing, so seeding would mark never-written tasks `DONE` and skip them
   forever. For a prefilled year, re-run the (resume-safe) cube build to
   rebuild a lost tracker, or use the tracker from the original build.
3. **Phase 2 — initial 2026 catch-up:** the first run builds and publishes
   `modis_archive_catalog_2026.parquet` for its window and ingests in
   ascending order, pre-filling the 2026 time axis (365 slots, marker
   `atlantis_time_prefill`) and establishing the tracker baseline. If the
   backlog exceeds a month, the catch-up guardrail (§4.1) chunks the run to
   the **next** 31 days from the watermark — re-running the same command
   continues automatically until the year is caught up, no explicit dates
   needed.
   The flow was validated end-to-end by the smoke test
   (`.kilo/plans/1786458113122-modis-update-smoketest.md`): dry-run window
   resolution, a one-day real-data run against local roots, and axis-probe
   verification, before the production kickoff archived the 31-day catch-up
   window with zero failures. In 2026-08 the initial 2026 data (Jul 6 – Aug 5)
   was deliberately wiped and rebuilt automatically: the first post-wipe run
   chunked forward from 2026-01-01 (`2026-01-01 → 2026-01-31`), re-prefilling
   the 365-slot axis on the fresh group, and subsequent runs continue chunk
   by chunk.
4. **Phase 3 — weekly runs:** start the VM and invoke
   `atlantis archive modis update`. Near real time the effective window is
   `last_complete + 1 - lookback` → `today - lag`; the lookback covers late
   LAADS publications and failed prior runs. At year rollover the job
   finishes outstanding December tasks in the old year before starting
   January tasks in the new one.

## 8. Archive invariants

1. **One writer per MODIS year** — the per-year lock serialises all updates.
2. **The tracker is the task-level source of truth**, not the latest Zarr time
   coordinate.
3. **The archive and tracker must agree** — a `DONE` task is trusted only when
   its date is present on the archive axis; a mismatch is a repair condition,
   never a reason to advance the watermark.
4. **SQLite stays on a local POSIX filesystem** — the tracker lives on the
   mounted state volume and is backed up to S3 after each run.
5. **Tracker lineage is stable** — a year's tracker is reused only with its
   canonical yearly catalogue; the manifest records the catalogue checksum. A
   changed source interpretation or replacement catalogue requires an explicit
   migration or a new tracker.
6. **A year is complete only when every expected task is `DONE`** with no
   unresolved `FAILED` tasks.

## 9. Out of scope

- VIIRS/GFM updates and a generic multi-source scheduler.
- Concurrent MODIS writers for the same archive year.
- Automatic deletion of archive dates, tracker rows, or orphaned data.
- Maintaining the full-history `modis_archive_catalog.parquet` (a future
  end-of-year process may derive it from the frozen yearly catalogues).
- ETag/fingerprint-based reprocessing of sources already marked `DONE`.

## 10. Next continuation run — worked example

The 2026 catch-up is resumable by design: each run ingests the next 31 days
from the watermark and stops. After the January run (watermark
`2026-01-31`), continuing costs exactly one command:

```text
PYTHONPATH=src pixi run -e batch python -m atlantis.cli archive modis update --year 2026
```

What it does:

1. Resolves the window from the tracker watermark: cursor `2026-02-01` →
   chunk `2026-02-01 → 2026-03-03`, with the printed guardrail notice
   ("re-run `update` to continue automatically"). No dates are ever passed.
2. Launches the worker in a detached tmux session
   (`atlantis-modis-update-2026-<runid>`); the log lands under
   `<state-root>/2026/logs/<runid>.log`.
3. Refreshes the catalogue for the chunk, reconciles (~7,900 pending tiles),
   preflights one download, then ingests through the ordered Dask batch
   (~5–7 h), reusing the existing tracker and prefilled 365-slot axis.
4. Validates, advances the watermark to the chunk's end, writes the `ok`
   manifest, and refreshes the S3 backup.

What to expect while it runs:

- `pixi run -e batch modis-archive-status -- --year 2026` shows the per-date
  heatmap and watermark live; February fills `#` day by day.
- A crash mid-run is safe: re-run the same command — `DONE` tiles are
  skipped and the window re-resolves from the watermark.
- Repeat the same command for each next chunk (March, April, …) until the
  watermark is within a chunk of `today - lag`, at which point the window
  automatically switches to the weekly lookback re-scan and the year runs
  near real time.

Rules that stay true on every continuation: never `seed-tracker` or delete
the tracker to "fix" a run, and never launch two writers for the same year
(the per-year lock would reject the second one).
