"""Build the GEOID-Flood events GFM Zarr archive.

Generates one task per (event-AoI, date, EQUI7 tile) for the full
GEOID-Flood event-AoI set (windows = metadata range + 14-day post-flood
pad), then streams them through the production GFM cube batch
(:func:`run_gfm_cube_batch`) into a new sparse Zarr cube — same schema/layout
as the year and KuroSiwo cubes, so the existing reader, STAC and viz tooling
work unchanged. Resume-safe via a per-run SQLite tracker: re-running skips
DONE tasks.

The task unit is the EQUI7 tile — GFM's native storage unit (one STAC item
per (tile, date), streamed as a whole COG from EODC). Each task carries one
tile's items for one date with the tile's own bbox, so every task reads
exactly the COGs it needs and writes one per-tile cell into the cube, exactly
like the year cubes. Events only select which tiles/dates are in scope.

Task items come from the per-year S3 catalogues where one exists (2021–2025),
and from live EODC STAC searches otherwise (GEOID-Flood spans 2016–2026, so
windows outside the catalogue years are searched day by day — days without
GFM items are skipped, mirroring the KuroSiwo build exactly, including the
empty pre-2021 searches). Items whose STAC metadata lacks a valid
``Equi7Tile``/bbox are recorded to ``<tasks>.dropped.json`` for post-run
coverage reconciliation.

Task ids embed the event, the native AoI, the tile and the date
(``gfm-EMSR712-10-EU020M_E036N009T3-20241029``) so two AoIs of one activation
never collide in the tracker.

Usage::

    PYTHONPATH=src pixi run -e batch python scripts/build_geoidflood_gfm_archive.py \
        --archive s3://atlantis/zarr/geoidflood_events \
        --db-path geoidflood_gfm_cube_tracker.db

Run detached (tmux) — an SSH disconnect must not stop the coordinator.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from atlantis.archive.cube_batch import run_gfm_cube_batch  # noqa: E402
from atlantis.batch import BatchConfig  # noqa: E402
from atlantis.fetchers.gfm.event_tasks import (  # noqa: E402
    build_tasks_from_catalogues,
    build_tasks_live,
    is_valid_tile,
    task_id,
)
from atlantis.utils.setup import AWS_PROFILES  # noqa: E402

AOI_TABLE = _REPO_ROOT / "data" / "metadata" / "geoidflood_aois.csv"
TASKS_ALL_PATH = _REPO_ROOT / "data" / "benchmark" / "gfm_aoi_tasks_geoidflood_all.json"


def generate_tasks() -> tuple[list[dict], list[dict]]:
    """Full GEOID-Flood task list: catalogues where available, live search otherwise."""
    aoi_table = pd.read_csv(AOI_TABLE)
    tasks, live_events = build_tasks_from_catalogues(aoi_table, task_id)
    dropped: list[dict] = []
    if live_events:
        live_tasks, dropped = build_tasks_live(aoi_table, live_events, task_id)
        tasks += live_tasks
    tasks.sort(key=lambda t: (t["event_id"], t["aoi_id"], t["date"], t["equi7_tile"]))
    return tasks, dropped


def write_tasks(path: Path, tasks: list[dict], dropped: list[dict]) -> None:
    """Write the task list, plus any dropped items to ``<path>.dropped.json``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tasks, indent=1))
    if dropped:
        drop_path = path.with_name(f"{path.stem}.dropped.json")
        drop_path.write_text(json.dumps(dropped, indent=1))
        print(f"  {len(dropped)} item(s) dropped for missing/invalid metadata → {drop_path}")


def load_or_build_tasks(path: Path) -> tuple[list[dict], list[dict]]:
    """Load the cached task list, regenerating it if it is stale or invalid.

    Cached task lists from before the per-tile task scheme (512-arcmin block
    tasks, ``equi7_tile`` = block id) are silently accepted by the batch
    engine, so they are detected here — via the EQUI7 tile-id format check —
    and rebuilt rather than run as-is.
    """
    if not path.exists():
        print("Building task list (catalogues + live search)…")
        tasks, dropped = generate_tasks()
        write_tasks(path, tasks, dropped)
        return tasks, dropped

    tasks = json.loads(path.read_text())
    stale = not isinstance(tasks, list) or any(
        not isinstance(t, dict) or not is_valid_tile(t.get("equi7_tile")) for t in tasks
    )
    if stale:
        print(
            f"WARNING: {path} contains tasks in the old 512-arcmin-block format; "
            "regenerating per-tile tasks. If the tracker DB was created by a previous "
            "block-based run, delete it so the new task ids are not skipped as DONE."
        )
        tasks, dropped = generate_tasks()
        write_tasks(path, tasks, dropped)
        return tasks, dropped
    return tasks, []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--archive", default="s3://atlantis/zarr/geoidflood_events", help="cube root (parent of datacube.zarr)"
    )
    parser.add_argument(
        "--db-path", type=Path, default=Path("geoidflood_gfm_cube_tracker.db"), help="SQLite resume tracker"
    )
    parser.add_argument("--workers", type=int, nargs=2, default=[2, 6], help="Dask workers min max")
    parser.add_argument("--memory-limit", default="4GB", help="memory cap per worker")
    parser.add_argument("--tasks-only", action="store_true", help="only generate the task list, do not run the batch")
    parser.add_argument("--tasks", type=Path, default=TASKS_ALL_PATH, help="task list (built if missing)")
    args = parser.parse_args()

    tasks, dropped = load_or_build_tasks(args.tasks)
    print(f"Tasks: {len(tasks):,} ({sum(len(t['item_hrefs']) for t in tasks):,} items)")

    if args.tasks_only:
        return

    storage_options = None
    if args.archive.startswith("s3://"):
        ecmwf_profile = next((p for p in AWS_PROFILES if p.name == "default"), None)
        if ecmwf_profile is None or not ecmwf_profile.endpoint_url:
            raise SystemExit("The 'default' AWS profile is not configured. Run `atlantis setup` first.")
        storage_options = {"endpoint_url": ecmwf_profile.endpoint_url}

    cfg = BatchConfig(
        db_path=args.db_path,
        workers_min=args.workers[0],
        workers_max=args.workers[1],
        memory_limit_per_worker=args.memory_limit,
        dashboard_port=8796,
        log_every=20,
    )

    t0 = time.monotonic()
    final = run_gfm_cube_batch(tasks, archive_root=args.archive, cfg=cfg, storage_options=storage_options)
    print(
        f"DONE={final.get('DONE', 0)} FAILED={final.get('FAILED', 0)} of {len(tasks)} "
        f"in {(time.monotonic() - t0) / 3600:.1f}h → {args.archive}"
    )


if __name__ == "__main__":
    main()
