"""Benchmark GFM processing of KuroSiwo event AOIs.

Answers: how heavy is one (AOI, date) task, and how many run in parallel on
this host? Three subcommands, sharing a task-JSON exchange file:

* ``sample`` — pick 8 deterministic test events (2 largest extents, 2
  smallest, 4 spread across latitude bands) and build tasks from the AOI
  table (one per (AOI, date), items found via live EODC STAC searches)
  → ``data/benchmark/gfm_aoi_tasks.json``.
* ``run-a`` — process the tasks sequentially through the production produce
  function (:func:`atlantis.fetchers.gfm.batch_processor.harmonise_gfm_payload`),
  recording per-task wall time, item count, peak RSS (VmHWM) and output
  shape → ``data/benchmark/gfm_aoi_run_a.csv``.
* ``run-b`` — the same tasks through the production Dask batch engine
  (:func:`atlantis.archive.cube_batch.run_cube_batch`) at a given worker
  count, with its own SQLite tracker per sweep → one summary row per sweep
  in ``data/benchmark/gfm_aoi_run_b.csv`` plus a projected full-event-set
  runtime (11,773 tasks from the legacy 512-arcmin-block AOI estimate).

Usage::

    PYTHONPATH=src pixi run -e batch python scripts/benchmark_gfm_aois.py sample
    PYTHONPATH=src pixi run -e batch python scripts/benchmark_gfm_aois.py run-a
    PYTHONPATH=src pixi run -e batch python scripts/benchmark_gfm_aois.py run-b --workers 2 4 6
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from atlantis.fetchers.gfm.backend import GfmStacBackend  # noqa: E402
from atlantis.models.event import FloodEvent  # noqa: E402

AOI_TABLE = _REPO_ROOT / "data" / "metadata" / "kurosiwo_aois.csv"
BENCH_DIR = _REPO_ROOT / "data" / "benchmark"
TASKS_PATH = BENCH_DIR / "gfm_aoi_tasks.json"
RUN_A_CSV = BENCH_DIR / "gfm_aoi_run_a.csv"
RUN_B_CSV = BENCH_DIR / "gfm_aoi_run_b.csv"
N_SAMPLE_EVENTS = 8
TOTAL_TASKS_FULL_SET = 11773  # legacy 512-arcmin-block estimate (Σ dates × blocks) from estimate_kurosiwo_aois.py


# ── Sample selection ─────────────────────────────────────────────────────────


def select_sample_events(aoi_table: pd.DataFrame, n: int = N_SAMPLE_EVENTS) -> list[str]:
    """Deterministic pick: 2 largest, 2 smallest, rest spread across lat bands.

    Extent is computed per row (the current AOI table has one row per event,
    so a max-minus-min span over the group is always zero) and aggregated as
    the per-event maximum.
    """
    per_row = aoi_table.assign(
        extent=(aoi_table["aoi_east"] - aoi_table["aoi_west"]) * (aoi_table["aoi_north"] - aoi_table["aoi_south"])
    )
    per_event = (
        per_row.groupby("event_id")
        .agg(
            lon_span=("aoi_east", lambda s: s.max() - s.min()),
            lat_span=("aoi_north", lambda s: s.max() - s.min()),
            centroid_lat=("aoi_north", lambda s: s.max() - s.min()),
            n_blocks=("aoi_id", "count"),
            extent=("extent", "max"),
        )
        .reset_index()
    )
    per_event["centroid_lat"] = (
        aoi_table.groupby("event_id").apply(lambda g: (g["aoi_south"].min() + g["aoi_north"].max()) / 2).values
    )
    per_event = per_event.sort_values("extent")

    chosen: list[str] = []
    for candidate in per_event.head(2)["event_id"]:  # smallest
        chosen.append(candidate)
    for candidate in per_event.tail(2)["event_id"]:  # largest
        chosen.append(candidate)

    bands = np.quantile(per_event["centroid_lat"], [0.25, 0.5, 0.75])
    for band in [(-np.inf, bands[0]), (bands[0], bands[1]), (bands[1], bands[2]), (bands[2], np.inf)]:
        in_band = per_event[
            per_event["centroid_lat"].between(band[0], band[1], inclusive="both") & ~per_event["event_id"].isin(chosen)
        ]
        if not in_band.empty:
            chosen.append(in_band.sort_values("extent").iloc[-1]["event_id"])
    return chosen[:n]


def sample_dates(start: str, end: str, max_dates: int) -> list[str]:
    """Up to *max_dates* dates, always including the flood date (window end)."""
    d0 = date.fromisoformat(start)
    days = (date.fromisoformat(end) - d0).days + 1
    if days <= max_dates:
        return [(d0 + timedelta(days=i)).isoformat() for i in range(days)]
    idx = np.unique(np.linspace(0, days - 1, max_dates, dtype=int))
    return [(d0 + timedelta(days=int(i))).isoformat() for i in idx]


def task_id_for(row, day: str) -> str:
    """Task id embedding the AOI table's key columns (event, AoI, block when present).

    The fallback embeds ``event_id`` too: with the block-free AOI tables,
    ``aoi_id`` is not globally unique (GEOID-Flood AoI numbers repeat across
    activations), so without it two events on the same date collide in the
    tracker and get silently deduped.
    """
    if "block_id" in row._fields:
        return f"gfm-{row.event_id}-{row.aoi_id}-{row.block_id}-{day.replace('-', '')}"
    return f"gfm-{row.event_id}-{row.aoi_id}-{day.replace('-', '')}"


def build_tasks(
    aoi_table: pd.DataFrame,
    events: list[str] | None,
    max_dates: int,
) -> tuple[list[dict], int]:
    """Live-search STAC per (AOI, date) and build batch tasks (see module docstring)."""
    backend = GfmStacBackend()
    tasks: list[dict] = []
    skipped_dates = 0
    rows = aoi_table[aoi_table["event_id"].isin(events)] if events else aoi_table
    for row in rows.itertuples(index=False):
        equi7_tile = row.block_id if "block_id" in row._fields else row.aoi_id
        for day in sample_dates(row.date_start, row.date_end, max_dates):
            items = backend.search(
                FloodEvent(
                    event_id=row.event_id,
                    bbox=(row.aoi_west, row.aoi_south, row.aoi_east, row.aoi_north),
                    start_date=date.fromisoformat(day),
                    end_date=date.fromisoformat(day),
                )
            )
            if not items:
                skipped_dates += 1
                continue
            tasks.append(
                {
                    "task_id": task_id_for(row, day),
                    "date": day,
                    "equi7_tile": equi7_tile,
                    "item_hrefs": [item.self_href for item in items],
                    "bbox": [float(row.aoi_west), float(row.aoi_south), float(row.aoi_east), float(row.aoi_north)],
                    "event_id": row.event_id,
                    "aoi_id": row.aoi_id,
                }
            )
            print(f"  {row.event_id} {equi7_tile} {day}: {len(items)} items")
    return tasks, skipped_dates


# ── Measurements ─────────────────────────────────────────────────────────────


def vm_hwm_kb() -> int:
    """Peak resident set size (kB) of this process, from /proc."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def cap_tasks_by_event(tasks: list[dict], max_tasks: int) -> list[dict]:
    """First whole events' tasks up to *max_tasks* (deterministic subset)."""
    if max_tasks <= 0:
        return tasks
    kept: list[dict] = []
    for _, event_tasks in itertools.groupby(tasks, key=lambda t: t["event_id"]):
        kept.extend(event_tasks)
        if len(kept) >= max_tasks:
            return kept[:max_tasks]
    return kept


def run_a(tasks: list[dict], out_csv: Path, max_tasks: int) -> None:
    from atlantis.archive.grid import coords_to_window
    from atlantis.fetchers.gfm.batch_processor import harmonise_gfm_payload

    rows: list[dict] = []
    tasks = cap_tasks_by_event(tasks, max_tasks)
    for task in tasks:
        t0 = time.monotonic()
        payload = harmonise_gfm_payload(task)
        wall = time.monotonic() - t0
        shape = payload["water_fraction"].shape
        window = coords_to_window(payload["y"], payload["x"])
        aligned = (window.height, window.width) == shape
        rows.append(
            {
                "task_id": task["task_id"],
                "event_id": task["event_id"],
                "aoi_id": task["aoi_id"],
                "date": task["date"],
                "n_items": len(task["item_hrefs"]),
                "wall_s": round(wall, 1),
                "vmhwm_kb": vm_hwm_kb(),
                "shape": f"{shape[0]}x{shape[1]}",
                "grid_aligned": aligned,
            }
        )
        print(f"  {task['task_id']}: {len(task['item_hrefs'])} items, {wall:.1f}s, {shape}")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    times = [r["wall_s"] for r in rows]
    print(
        f"\nRun A: {len(rows)} tasks, total {sum(times):.0f}s, mean {np.mean(times):.1f}s, "
        f"median {np.median(times):.1f}s, max {max(times):.1f}s → {out_csv}"
    )


def run_b(tasks: list[dict], workers_list: list[int], out_csv: Path, max_tasks: int, memory_limit: str) -> None:
    from atlantis.archive.cube_batch import run_cube_batch
    from atlantis.batch import BatchConfig
    from atlantis.fetchers.gfm.batch_processor import harmonise_gfm_payload

    tasks = cap_tasks_by_event(tasks, max_tasks)
    print(f"Run B task subset: {len(tasks)} tasks ({len({t['event_id'] for t in tasks})} events)")

    def consume(payload: dict) -> str:
        return f"benchmark#{payload['task_id']}"

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for i, workers in enumerate(workers_list):
        db = BENCH_DIR / f"gfm_tracker_w{workers}.db"
        cfg = BatchConfig(
            db_path=db,
            workers_min=workers,
            workers_max=workers,
            memory_limit_per_worker=memory_limit,
            dashboard_port=8790 + i,
            log_every=10,
        )
        t0 = time.monotonic()
        try:
            final = run_cube_batch(tasks, harmonise_gfm_payload, consume, cfg)
        except Exception as exc:  # noqa: BLE001 - record sweep failure, keep going
            wall = time.monotonic() - t0
            print(f"Run B workers={workers}: sweep crashed after {wall:.0f}s ({exc})")
            rows.append(
                {
                    "workers": workers,
                    "n_tasks": len(tasks),
                    "wall_s": round(wall, 1),
                    "throughput_per_hr": None,
                    "done": None,
                    "failed": None,
                    "full_set_hrs": None,
                    "note": repr(exc)[:200],
                }
            )
            continue
        wall = time.monotonic() - t0
        throughput = len(tasks) / wall * 3600
        rows.append(
            {
                "workers": workers,
                "n_tasks": len(tasks),
                "wall_s": round(wall, 1),
                "throughput_per_hr": round(throughput, 1),
                "done": final.get("DONE", 0),
                "failed": final.get("FAILED", 0),
                "full_set_hrs": round(TOTAL_TASKS_FULL_SET / throughput, 1) if throughput else None,
            }
        )
        print(
            f"Run B workers={workers}: {len(tasks)} tasks in {wall:.0f}s "
            f"({throughput:.0f}/hr) DONE={final.get('DONE')} FAILED={final.get('FAILED')} "
            f"→ full 11,773-task set ≈ {rows[-1]['full_set_hrs']}h"
        )
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    print(f"Run B summary → {out_csv}")


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--aoi-table",
        type=Path,
        default=AOI_TABLE,
        help="AOI table CSV (default: data/metadata/kurosiwo_aois.csv; "
        "point at data/metadata/geoidflood_aois.csv to benchmark GEOID-Flood)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sample = sub.add_parser("sample", help="build the sample task list")
    p_sample.add_argument("--max-dates", type=int, default=12, help="max dates sampled per event window")
    p_sample.add_argument("--events", nargs="*", default=None, help="explicit event ids (default: 8-event sample)")
    p_sample.add_argument("--all", action="store_true", help="all events, full windows (archive build input)")

    p_a = sub.add_parser("run-a", help="sequential per-task benchmark")
    p_a.add_argument("--tasks", type=Path, default=TASKS_PATH)
    p_a.add_argument("--max-tasks", type=int, default=0, help="cap subset (whole events)")

    p_b = sub.add_parser("run-b", help="Dask concurrency sweep")
    p_b.add_argument("--workers", type=int, nargs="+", default=[2, 4, 6])
    p_b.add_argument("--tasks", type=Path, default=TASKS_PATH)
    p_b.add_argument("--max-tasks", type=int, default=0, help="cap subset (whole events)")
    p_b.add_argument("--memory-limit", default="8GB", help="memory cap per worker (GFM production default)")

    args = parser.parse_args()

    aoi_table = pd.read_csv(args.aoi_table)
    print(f"AOI table: {args.aoi_table} ({len(aoi_table)} rows)")

    if args.cmd == "sample":
        if args.all:
            tasks, skipped = build_tasks(aoi_table, events=None, max_dates=10_000)
        else:
            events = args.events or select_sample_events(aoi_table)
            print(f"Sample events: {events}")
            tasks, skipped = build_tasks(aoi_table, events=events, max_dates=args.max_dates)
        BENCH_DIR.mkdir(parents=True, exist_ok=True)
        TASKS_PATH.write_text(json.dumps(tasks, indent=1))
        n_items = sum(len(t["item_hrefs"]) for t in tasks)
        print(f"Tasks: {len(tasks)} ({skipped} date(s) with no items), {n_items} items total → {TASKS_PATH}")
        return

    tasks = json.loads(args.tasks.read_text())
    print(f"Loaded {len(tasks)} tasks from {args.tasks}")

    if args.cmd == "run-a":
        run_a(tasks, RUN_A_CSV, args.max_tasks)
    elif args.cmd == "run-b":
        run_b(tasks, args.workers, RUN_B_CSV, args.max_tasks, args.memory_limit)


if __name__ == "__main__":
    main()
