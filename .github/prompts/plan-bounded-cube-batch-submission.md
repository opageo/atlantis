# Plan: Bound Dask Submission for Cube Batches

## Goal

Prevent the Dask scheduler from holding every pending task/future for a full-catalogue cube run.

Scope: `run_cube_batch` only, which serves **GFM, VIIRS, and MODIS cube ingestion**.

Do **not** change source processors, Zarr writing, task IDs, tracker semantics, worker defaults, or retry counts.

## Current behavior

`src/atlantis/archive/cube_batch.py` submits every pending task at once:

```python
futures = client.map(produce_fn, pending, retries=cfg.retries, pure=False)
for future in as_completed(futures):
    ...
    future.release()
```

`future.release()` correctly frees a completed payload, but the scheduler still receives the complete task graph upfront. For the 2025 GFM catalogue this is about **205,635 cell tasks**.

Also preserve the existing choice to **not scatter task dictionaries**: inputs must remain scheduler-owned so retries still work after a Nanny restart.

## Required code change

Refactor `run_cube_batch` to maintain a fixed number of submitted-but-unfinished tasks.

Use:

```python
max_in_flight = max(1, 4 * cfg.workers_max)
```

Keep this as a local implementation detail initially—do **not** add a new CLI option or `BatchConfig` field unless profiling later shows a need.

### Algorithm

Inside the existing `with Client(cluster) as client:` block:

1. Create:
   - `pending_iter = iter(pending)`
   - `completed = as_completed()`
   - `key_to_id: dict[str, str] = {}`
2. Define a small local helper to:
   - take one task from `pending_iter`;
   - submit it with:
     ```python
     client.submit(produce_fn, task, retries=cfg.retries, pure=False)
     ```
   - register the future with `completed.add(future)`;
   - record `key_to_id[future.key] = task["task_id"]`.
3. Fill the initial window with up to `max_in_flight` tasks.
4. Iterate over `completed`:
   - obtain `future.result()`;
   - run the existing coordinator-side `consume(payload)`;
   - write `DONE` or `FAILED` to SQLite exactly as today;
   - always call `future.release()` in `finally`;
   - remove its entry from `key_to_id`;
   - submit exactly one replacement task, if any remain.
5. Exit only when the `as_completed` collection is empty and the iterator is exhausted.

This keeps at most $4 \times \text{workers_max}$ task inputs and results live in the scheduler/client path, while keeping workers supplied with work.

For GFM defaults this means at most:

$$
4 \times 3 = 12
$$

in-flight cells, instead of approximately 205,635.

## Correctness constraints

The implementation must preserve:

- `DONE` only after `consume()` succeeds.
- `FAILED` if either worker production or coordinator consumption fails.
- `future.release()` for success and failure.
- retries through `retries=cfg.retries`.
- resume behavior: pre-existing `DONE` tasks are skipped.
- scheduler-owned task literals—**no `client.scatter()`**.
- serial coordinator writes to the Zarr session.
- current progress logging and final tracker statistics.

Do not use a dependency chain between independent tasks. The bounded window is only submission backpressure.

## Tests to add or update

Update `tests/archive/test_cube_batch_unit.py`.

1. **Existing success/failure/resume tests**
   - Adapt the fake Dask client from `client.map()` to `client.submit()`.
   - Preserve assertions for tracker output and released futures.

2. **Bounded submission test**
   - Set `cfg.workers_max = 2`, so the cap is 8.
   - Provide at least 10–12 tasks.
   - Assert only 8 tasks are submitted before the first completion.
   - Assert one replacement is submitted after each processed completion.
   - Assert all tasks eventually become `DONE`.

3. **Maximum in-flight test**
   - Record active futures in the fake `as_completed` implementation.
   - Assert the active count never exceeds `4 * cfg.workers_max`.

4. **Failure replenishment test**
   - Make one submitted future fail.
   - Confirm it is marked `FAILED`, released, and replaced by the next pending task.
   - Confirm later tasks still complete.

5. **Resume test**
   - Seed some `DONE` rows.
   - Confirm only remaining tasks are submitted and the initial window is calculated from those remaining tasks.

The existing `future.release()` test should remain.

## Validation

Run:

```bash
PYTHONPATH=src pixi run -e batch pytest -q tests/archive/test_cube_batch_unit.py
```

Then run Ruff on changed Python files:

```bash
pixi run -e batch ruff check src/atlantis/archive/cube_batch.py tests/archive/test_cube_batch_unit.py
```

Real-batch smoke validation:

```bash
rm -rf tmp_gfm_test_cube
rm -f gfm_cube_tracker_test.db

PYTHONPATH=src pixi run -e batch python -m atlantis.cli batch gfm cube run \
  --inventory s3://atlantis/assets/gfm/gfm_archive_catalog_2025.parquet \
  --partition 0:100 \
  --db-path gfm_cube_tracker_test.db \
  --archive ./tmp_gfm_test_cube \
  --log-every 1
```

Acceptance:

- `DONE=48`
- `FAILED=0`
- no large-graph warning
- Dask dashboard shows only a bounded number of queued tasks
- no regression in worker memory or Nanny restarts

## Expected impact

**GFM:** valuable before a full 2025 ingestion; lowers scheduler overhead for ~205k cells.

**VIIRS/MODIS cube runs:** beneficial and output-neutral because they use the same `run_cube_batch` path. Their work remains parallel; only the number of queued tasks becomes bounded.

**Generic COG batches:** explicitly out of scope for this change. `src/atlantis/batch/orchestrator.py` still uses `scatter()` and lacks `future.release()`. That should be a separate follow-up under issue #118, because its result/tracker model differs from cube ingestion.
