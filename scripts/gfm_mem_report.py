"""Report on the GFM 2023 memory tracker output.

Reads the tracker CSV + summary JSONL and prints a digest:

* per-worker peak / median / last RSS and Dask-reported memory,
* aggregate peaks and whether any worker crossed Dask pause (80 %) /
  terminate (95 %) thresholds of the configured memory limit,
* nanny restart events (pid changes),
* batch progress (DONE count from the SQLite tracker) with an ETA.

Usage:
    python gfm_mem_report.py [--csv ...] [--summary ...] [--db ...]
                             [--limit 12GB] [--workers 2] [--window 300]
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from dask.utils import parse_bytes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="/tmp/kilo/gfm_mem_tracker_2023.csv")
    ap.add_argument("--summary", default="/tmp/kilo/gfm_mem_tracker_2023.summary.jsonl")
    ap.add_argument("--db", default="/home/slagaras/atlantis/archive_tracker_gfm_2023.db")
    ap.add_argument("--limit", default="12GB", help="configured memory limit per worker")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--window", type=float, default=300, help="recent window (s) for trend")
    args = ap.parse_args()

    limit = parse_bytes(args.limit)
    pause_at = 0.8 * limit
    terminate_at = 0.95 * limit

    rows = list(csv.DictReader(open(args.csv)))
    summaries = [json.loads(line) for line in open(args.summary) if line.strip()]
    if not rows:
        print("no samples yet")
        return 1

    workers: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        workers[r["worker"]].append(r)

    last_ts = max(float(r["ts"]) for r in rows)
    first_ts = min(float(r["ts"]) for r in rows)
    span = last_ts - first_ts

    print(f"samples: {len(rows)} over {span:.0f}s (first {summaries[0]['iso']} last {summaries[-1]['iso']})")
    print(
        f"memory limit/worker: {args.limit}  pause=80%={pause_at / 1e6:.0f}MB  terminate=95%={terminate_at / 1e6:.0f}MB"
    )
    print()

    total_peak_rss = 0.0
    nanny_events: list[str] = []
    for worker, rs in sorted(workers.items()):
        rs_sorted = sorted(rs, key=lambda x: float(x["ts"]))
        peak = max(float(r["rss_mb"]) for r in rs)
        peak_dask = max(float(r["dask_memory_mb"]) for r in rs)
        latest = float(rs_sorted[-1]["rss_mb"])
        med = sorted(float(r["rss_mb"]) for r in rs)[len(rs) // 2]
        recent = [float(r["rss_mb"]) for r in rs if float(r["ts"]) >= last_ts - args.window]
        recent_peak = max(recent) if recent else 0.0
        pids = {r["pid"] for r in rs if r["pid"]}
        total_peak_rss += peak

        crosses = []
        if peak >= terminate_at / 1e6:
            crosses.append(f"**TERMINATE threshold {terminate_at / 1e6:.0f}MB CROSSED**")
        elif peak >= pause_at / 1e6:
            crosses.append(f"PAUSE threshold {pause_at / 1e6:.0f}MB crossed")
        print(f"worker {worker}  pid={pids or '?'}")
        print(
            f"  rss  peak={peak:8.0f}MB  med={med:8.0f}MB  last={latest:8.0f}MB  "
            f"recent{args.window:.0f}s peak={recent_peak:8.0f}MB"
        )
        print(f"  dask peak={peak_dask:8.0f}MB  {' | '.join(crosses) if crosses else 'within budget'}")
        print()
        if len(pids) > 1:
            nanny_events.append(f"{worker}: pids seen {sorted(pids)}")

    print("=== aggregate ===")
    print(
        f"sum of per-worker RSS peaks: {total_peak_rss:8.0f}MB  "
        f"({total_peak_rss / 1e3:.2f} GB over {args.workers} workers)"
    )
    print(f"latest summary: {summaries[-1]}")
    if nanny_events:
        print("=== NANNY RESTART EVENTS ===")
        for e in nanny_events:
            print("  " + e)
    else:
        print("no worker restarts detected")

    # Batch progress from the sqlite tracker
    if Path(args.db).exists():
        con = sqlite3.connect(args.db)
        counts = dict(con.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status"))
        done = counts.get("DONE", 0)
        failed = counts.get("FAILED", 0)
        # total tasks = unique (date, tile) in the catalog
        import pandas as pd

        df = pd.read_parquet(
            "s3://atlantis/assets/gfm/gfm_archive_catalog_2023.parquet",
            storage_options={},
        )
        total = df.groupby(["date", "equi7_tile"]).ngroups
        rate = done / span * 3600 if span > 0 else 0
        remaining_hr = (total - done) / rate if rate > 0 else float("inf")
        print()
        print("=== batch progress ===")
        print(f"done={done} failed={failed} total={total} ({100 * done / total:.2f}%)")
        print(f"rate≈{rate:.0f} tasks/hr → ETA≈{remaining_hr:.1f} hr ({remaining_hr / 24:.1f} d)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
