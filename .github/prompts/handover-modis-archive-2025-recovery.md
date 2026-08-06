# Handover: MODIS Archive Update — 2025 reindex incident & recovery

> **Read this first.** A previous agent (Kilo session, branch `archive-updating`)
> implemented the incremental MODIS archive update feature and was mid-way
> through filling the 2025 gap when an S3 swap failure left the 2025 `modis`
> group absent from the object store. **The data is NOT lost** — it exists in
> a temp group `_modis_sorted` on the store — but the group must be promoted
> back to `modis` before anything else runs against 2025.
>
> **Do NOT run any write operations without reading §1–§4.** The store
> (`object-store.os-api.cci1.ecmwf.int`) has flaky LISTING consistency, which
> caused this incident and the earlier 2023 "missing group" scare.

---

## 1. Incident state (last observed 2026-08-03 ~20:25 UTC)

- `s3://atlantis/zarr/2025/datacube.zarr/` contains: `gfm/`, `viirs/`,
  `_modis_sorted/`, `zarr.json`. **There is no `modis/` directory.**
- `_modis_sorted/` holds the complete reindexed 2025 MODIS data — 333 time
  slots (2025-01-01 … 2025-12-31, gap-free), all 4 arrays
  (`water_fraction`, `exclusion_mask`, `reference_water`, `recurring_flood`)
  plus `time/y/x/crs`. This is the CORRECT final state of the group, just
  under the wrong name.
- **A recovery `fs.copy(_modis_sorted → modis, recursive=True)` was STARTED
  and then ABORTED by the user mid-flight. The copy may be partial.** The
  next agent MUST check `s3://atlantis/zarr/2025/datacube.zarr/modis/`
  (existence, file count vs `_modis_sorted`) before doing anything.
- No `atlantis.cli` processes were running at abort time (only a stale
  `gfm-cube` tmux session from July remains; it is idle — safe to kill).
- The 2025 tracker is seeded at `/mnt/atlantis-state/modis/2025/cube_tracker.db`
  (76,934 DONE; 55 gap dates pending). 2023/2024 trackers also seeded and
  complete.

### What happened (root cause)

1. `archive modis _reindex-time --year 2025` builds a temp group
   `_modis_sorted` (copy + insert missing slots), then swaps it over `modis`.
2. The original swap used `store.fs` — a zarr **async-mode** s3fs — and
   `fs.rm` raised `RuntimeError: Loop is not running` (fixed: use a fresh
   synchronous `s3fs.S3FileSystem(**storage_options)`).
3. A second bug: `s3fs.mv` = `copy(on_error="raise")` + `rm(src)`. With this
   store's flaky listings the copy can silently see an EMPTY source listing
   and copy nothing without raising; meanwhile `rm(modis)` (which runs BEFORE
   the mv in `_swap_group`) had already deleted the real group. Result:
   `modis` gone, `_modis_sorted` intact, command printed success.
4. Also observed: `zarr.consolidate_metadata` on this store intermittently
   writes metadata missing groups (same listing-consistency issue — it caused
   the earlier "2023 has no modis group" scare, fixed by re-consolidation).

### Evidence / prior fixes already committed

- The swap fix (sync fs + retries) and the resume shortcut are in the working
  tree but **NOT yet committed** (see §7). The retry loop did NOT save us
  because the failure was a silent no-op, not an exception.
- `git log --oneline -5` on `archive-updating`:
  `2b0bfb2 docs… · 41af944 chore(pixi)… · d7714bd test… · b888fad feat(cli)… · ab0c55b feat(archive)…`

---

## 2. Immediate next steps (in order)

### 2.1 Promote the temp group (recovery)

Check state first (read-only):

```bash
PYTHONPATH=src pixi run python - <<'EOF'
from atlantis.utils.setup import AWS_PROFILES
p = next(x for x in AWS_PROFILES if x.name == 'default')
import s3fs
fs = s3fs.S3FileSystem(endpoint_url=p.endpoint_url)
base = 's3://atlantis/zarr/2025/datacube.zarr'
print('modis exists:', fs.exists(f'{base}/modis/zarr.json'))
print('modis files:', len(fs.find(f'{base}/modis')) if fs.exists(f'{base}/modis') else 0)
print('temp files:', len(fs.find(f'{base}/_modis_sorted')))
EOF
```

- If `modis` already has the same file count as `_modis_sorted` → the aborted
  copy finished; skip to 2.2.
- If `modis` is missing or partial → re-run the copy **with verification**:

```bash
PYTHONPATH=src pixi run python - <<'EOF'
from atlantis.utils.setup import AWS_PROFILES
p = next(x for x in AWS_PROFILES if x.name == 'default')
import s3fs, time
fs = s3fs.S3FileSystem(endpoint_url=p.endpoint_url)
base = 's3://atlantis/zarr/2025/datacube.zarr'
src, dst = f'{base}/_modis_sorted', f'{base}/modis'
n = len(fs.find(src))
fs.copy(src, dst, recursive=True)          # idempotent PUTs; safe to re-run
for i in range(12):
    n_dst = len(fs.find(dst))
    if n_dst >= n: break
    print(f'{i}: {n_dst}/{n} — waiting for listing consistency'); time.sleep(5)
assert len(fs.find(dst)) >= n, 'copy incomplete — abort, temp group still intact'
assert fs.exists(f'{dst}/zarr.json')
print('promote verified:', n)
EOF
```

Do **NOT** delete `_modis_sorted` yet — keep it as the recovery source until
the promoted `modis` group passes verification (2.2).

### 2.2 Verify the promoted group (before deleting temp)

```bash
PYTHONPATH=src pixi run python - <<'EOF'
from atlantis.utils.setup import AWS_PROFILES
p = next(x for x in AWS_PROFILES if x.name == 'default')
from atlantis.archive._store import store_for
from atlantis.archive import datacube, grid
import numpy as np
from datetime import date, timedelta
from atlantis.fetchers.modis.processor import tile_bounds_from_hv
store = store_for('s3://atlantis/zarr/2025', 'datacube.zarr', {'endpoint_url': p.endpoint_url})
g = datacube.open_root(store, mode='r')['modis']     # fails until metadata re-consolidated — see 2.3
t = np.asarray(g['time'][:], dtype='int64')
axis = sorted(date(2020,1,1) + timedelta(days=int(x)) for x in t)
full = {date(2025,1,1)+timedelta(days=i) for i in range(365)}
print('axis:', len(axis), '| missing:', len(full - set(axis)))
# spot-check a date that was previously a gap:
idx = {d:i for i,d in enumerate(axis)}
win = g['water_fraction'][idx[date(2025,3,1)], 5000:5300, 8000:8300]
print('2025-03-01 non-NODATA in sample window:', int(np.count_nonzero(win != 255)))
EOF
```

Expect: 333 slots, 0 missing, sample window has data.

### 2.3 Re-consolidate metadata (verify it sticks)

`zarr.consolidate_metadata` is flaky on this store — loop until the group is
visible, then DELETE `_modis_sorted`:

```bash
PYTHONPATH=src pixi run python - <<'EOF'
from atlantis.utils.setup import AWS_PROFILES
p = next(x for x in AWS_PROFILES if x.name == 'default')
from atlantis.archive._store import store_for
from atlantis.archive import datacube
import zarr, time, s3fs
store = store_for('s3://atlantis/zarr/2025', 'datacube.zarr', {'endpoint_url': p.endpoint_url})
for i in range(6):
    datacube.consolidate(store); time.sleep(3)
    check = zarr.open_group(store, mode='r')
    if 'modis' in check: print('consolidated OK'); break
    print(f'attempt {i}: modis missing from consolidated metadata — retrying')
assert 'modis' in zarr.open_group(store, mode='r')
fs = s3fs.S3FileSystem(endpoint_url=p.endpoint_url)
fs.rm('s3://atlantis/zarr/2025/datacube.zarr/_modis_sorted', recursive=True)  # only now
EOF
```

### 2.4 Re-run `status` and the update

```bash
PYTHONPATH=src pixi run python -m atlantis.cli archive modis status --year 2025 \
  --state-root /mnt/atlantis-state/modis
# expect: 333 archive dates, missing ranges: none (55 gap dates now slots, still pending)
```

Then fill the 55 gap dates (long-running — launch detached; ~15,840 tiles,
several hours; needs `EARTHDATA_TOKEN` which is in `.env` and loaded by the
CLI):

```bash
tmux new -s update2025
PYTHONPATH=src pixi run -e batch python -m atlantis.cli archive modis update --year 2025 --foreground
# watch: tmux attach -t update2025 ; verify: status --year 2025
```

The update resolves the window from the watermark (2025-02-17), processes only
the pending gap dates, skips DONE via the tracker. **Do not use
`batch modis cube run` for this** (re-downloads everything; completion-order
writer can leave the axis unsorted).

---

## 3. Hard-won environment facts

- Object store: `https://object-store.os-api.cci1.ecmwf.int`; the repo's
  `default` AWS profile (from `atlantis utils.setup AWS_PROFILES`) has the
  endpoint. `aws s3 ls` needs `--endpoint-url`; repo code uses
  `storage_options={"endpoint_url": ...}`.
- **Listing consistency is flaky** (S3-listing may lag object PUTs). Any
  copy/mv/consolidate MUST verify existence/counts and retry. Never
  `rm` before a verified copy of the replacement.
- `zarr.FsspecStore.fs` is an **async-mode** s3fs: sync calls raise
  `RuntimeError: Loop is not running`. Use a fresh
  `s3fs.S3FileSystem(**storage_options)` for direct fs ops.
- `zarr.open_group` auto-uses consolidated metadata when present; pass
  `use_consolidated=False` when you must see groups not in the consolidated
  root (e.g. temp groups).
- The `batch` pixi env has Dask + all geo deps; the `default` env does NOT
  (update/cube commands must run with `-e batch`). `pixi run python` =
  default env.
- State root: `/mnt/atlantis-state/modis/<year>/` (created via
  `sudo mkdir -p … && sudo chown $USER …`; default in CLI). A mirror of the
  seeded trackers exists at `/home/ykalfas/atlantis-state/modis/` (same
  content, created earlier — idempotent seed).
- Archive layout: one cube per year `s3://atlantis/zarr/<year>/datacube.zarr`
  with per-source groups `modis|viirs|gfm`. 2023's store ALSO holds a legacy
  multi-year layout quirk (its consolidation was stale — already fixed).

## 4. Known archive state (as of this handover)

| Year | Archive                                                                         | Tracker                                                          | Notes                                                                                                                                                          |
| ---- | ------------------------------------------------------------------------------- | ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2023 | 365/365 dates, complete; one repaired day (2023-02-18, 37 tiles refilled)       | seeded 102,363 DONE                                              | legacy `batch modis cube run` rebuild was killed (redundant); its leftover `archive_tracker_modis_2023.db` + `modis_2023_cube.log` in repo root can be deleted |
| 2024 | 366/366 dates, complete                                                         | seeded 102,452 DONE                                              |                                                                                                                                                                |
| 2025 | **INCIDENT**: `modis` group missing; data intact in `_modis_sorted` (333 slots) | seeded 76,934 DONE; 55 gap dates (2025-02-18…2025-04-13) pending | reindex succeeded; swap failed silently; recovery in progress (§2)                                                                                             |

2025's 55 gap dates are genuinely absent from the archive (both catalogues
list them; LAADS still serves them — verified). 2025 also has 32 "no data"
days (2025-06-17…2025-07-22) that are NOT in any catalogue (LAADS had
nothing) — nothing to do for them.

## 5. Feature overview (what the next agent inherits)

`atlantis archive modis …` — incremental, resume-safe weekly updates:

- `update` — launcher (detached tmux by default, `--foreground` for CI) →
  spawns `_run-update` worker: window resolution (watermark + lookback/lag) →
  yearly-catalogue refresh (candidate-then-promote, dedupe `(date,h,v)`) →
  reconcile (requeue DONE-but-missing; report orphans) → ordered batch
  (ascending time axis via `OrderedConsume`) → validation → contiguous
  watermark → immutable manifest → S3 backup (finally).
- `status` — per-year heatmap (`#` done / `x` failed / `o` pending / `.` no
  data), state detail (day counts + ranges), all-years monthly overview.
- `seed-tracker --year Y` — build a tracker from the archive (marks DONE all
  catalogue tasks whose date is on the axis; gap dates stay pending).
  Idempotent. Used to onboard pre-update years (2023/2024/2025).
- `_reindex-time --year Y` — one-off: insert missing date slots + sort axis
  via temp group + swap (THE COMMAND THAT NEEDS THE §2 FIX BEFORE REUSE).
- Pixi tasks: `modis-archive-update`, `modis-archive-update-dry-run`,
  `modis-archive-status`, `modis-archive-seed-tracker`.

Key modules: `src/atlantis/archive/update.py` (orchestration),
`ordering.py` (OrderedConsume + unsorted_spans),
`reindex_time.py` (migration — swap needs hardening, see §7),
`src/atlantis/batch/tracker.py` (`requeue` helper).
Docs: `docs/archive/modis-archive-update.md`, `docs/cli.md`, `docs/archive/cube-build.md §4.4`.

## 6. Invariants (do not violate)

1. One writer per MODIS year (per-year `update.lock`).
2. Tracker = task-level source of truth; a DONE task is trusted only when its
   date is on the archive axis.
3. SQLite stays on the local volume; backed up to S3 after each run.
4. Time axis strictly ascending; an older missing date is NEVER appended at
   the end (append-only policy → `_reindex-time` first).
5. Watermark advances only through contiguous complete dates.

## 7. Uncommitted work in the working tree

`git status` (as of handover): modified `src/atlantis/archive/reindex_time.py`,
`src/atlantis/cli.py`, `tests/archive/test_update.py` — the swap fix and
resume shortcut are NOT yet committed. **Before committing:**

1. Harden `_swap_group` in `reindex_time.py`:
   - Replace `fs.mv` with **verified copy + count check + retries**, then
     `fs.rm(temp)` — never `rm(old)` before the replacement is verified
     (see §2.1 pattern; this is exactly what the incident taught).
   - Consider `copy-overwrite` order (copy `new` onto `old` without deleting
     `old` first) so the group is never absent during the swap.
2. After `datacube.consolidate(store)`, verify the source group is in the
   consolidated root and retry (this store's listings lag).
3. Run `PYTHONPATH=src pixi run -e batch pytest -q tests/archive/test_update.py`
   (53 tests) and `pixi run -e batch ruff check src/ tests/`.
4. Pre-commit hooks run automatically on `git commit` (prettier reformats
   markdown — re-add and re-commit if it fails).
5. Commit style (conventional, scope): e.g.
   `fix(archive): verify remote group swap to tolerate lagging store listings`.
6. Push: `git push origin archive-updating` (branch tracks `origin`).

## 8. Pointers for the next agent

- Plan document (design): `.github/prompts/plan-modis-incremental-archive-update.md`
- Operational guide: `docs/archive/modis-archive-update.md`
- Gap-fill workflow: `docs/archive/cube-build.md §4.4`
- Tests: `tests/archive/test_update.py` (integration tests use local stores +
  a fake catalogue builder + fake batch engine; the S3 swap path is covered
  by a unit test with a fake fs).
- The 2025 gap-fill itself (§2.4) is the remaining production task; the
  weekly cadence thereafter is `pixi run -e batch modis-archive-update -- --year <current>`.
