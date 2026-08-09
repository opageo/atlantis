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

    PYTHONPATH=src pixi run -e events build-geoidflood-gfm-archive \
        --archive s3://atlantis/zarr/geoidflood_events \
        --db-path geoidflood_gfm_cube_tracker.db

Per-event backfill into the per-year cubes (the default path)::

    PYTHONPATH=src pixi run -e events build-geoidflood-gfm-archive \
        --year 2025 --events EMSR712-10 --db-path backfill_EMSR712_2025.db

``--year`` sets the archive to ``s3://atlantis/zarr/{year}`` (overriding
``--archive``), filters tasks to that calendar year, and pre-fills the gfm
group's ``time`` axis with all 366 days of the year so any event date lands
in a pre-existing slot — no ordering constraint, no reindex, and re-running
an event overwrites its cells in place (corrections). Events straddling a
year boundary are split across two runs (one ``--year`` each).

Run detached (tmux) — an SSH disconnect must not stop the coordinator.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from atlantis.archive import datacube  # noqa: E402
from atlantis.archive._store import store_for  # noqa: E402
from atlantis.archive.cube_batch import _to_date, run_gfm_cube_batch  # noqa: E402
from atlantis.archive.ordering import unsorted_spans  # noqa: E402
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


def generate_tasks(aoi_table: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """Full GEOID-Flood task list: catalogues where available, live search otherwise."""
    tasks, live_events = build_tasks_from_catalogues(aoi_table, task_id)
    dropped: list[dict] = []
    if live_events:
        live_tasks, dropped = build_tasks_live(aoi_table, live_events, task_id)
        tasks += live_tasks
    tasks.sort(key=lambda t: (t["event_id"], t["aoi_id"], t["date"], t["equi7_tile"]))
    return tasks, dropped


def filter_aoi_rows(table: pd.DataFrame, events: set[str]) -> pd.DataFrame:
    """Keep AOI rows matching *events* (event ids or ``event_id-aoi_id`` combos)."""
    if not events:
        return table
    ids = table["event_id"].astype(str)
    combos = ids + "-" + table["aoi_id"].astype(str)
    matching = table[ids.isin(events) | combos.isin(events)]
    if matching.empty:
        raise SystemExit(
            f"No AOI rows match --events {sorted(events)}. "
            f"Available event ids: {', '.join(sorted(ids.unique()))}"
        )
    return matching


def filter_tasks(tasks: list[dict], events: set[str], year: int | None) -> list[dict]:
    """Filter tasks by event (id or ``event_id-aoi_id``) and calendar year."""
    if events:
        tasks = [
            t
            for t in tasks
            if str(t.get("event_id")) in events
            or f"{t.get('event_id')}-{t.get('aoi_id')}" in events
        ]
    if year:
        tasks = [t for t in tasks if str(t["date"])[:4] == str(year)]
    return tasks


def filtered_tasks_path(path: Path, events: set[str], year: int | None) -> Path:
    """Sidecar path for a filtered task list (never overwrites the full cache)."""
    import hashlib

    suffix = "-".join(sorted(events))
    if year:
        suffix = f"{suffix}-{year}" if suffix else str(year)
    if len(f"{path.stem}-{suffix}.json") > 200:
        digest = hashlib.sha1(suffix.encode()).hexdigest()[:8]
        suffix = f"{len(events)}-events-{digest}" + (f"-{year}" if year else "")
    return path.with_name(f"{path.stem}-{suffix}.json")


def write_tasks(path: Path, tasks: list[dict], dropped: list[dict]) -> None:
    """Write the task list, plus any dropped items to ``<path>.dropped.json``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tasks, indent=1))
    if dropped:
        drop_path = path.with_name(f"{path.stem}.dropped.json")
        drop_path.write_text(json.dumps(dropped, indent=1))
        print(f"  {len(dropped)} item(s) dropped for missing/invalid metadata → {drop_path}")


def load_or_build_tasks(path: Path, aoi_table: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """Load the cached task list, regenerating it if it is stale or invalid.

    Cached task lists from before the per-tile task scheme (512-arcmin block
    tasks, ``equi7_tile`` = block id) are silently accepted by the batch
    engine, so they are detected here — via the EQUI7 tile-id format check —
    and rebuilt rather than run as-is.
    """
    if not path.exists():
        print("Building task list (catalogues + live search)…")
        tasks, dropped = generate_tasks(aoi_table)
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
        tasks, dropped = generate_tasks(aoi_table)
        write_tasks(path, tasks, dropped)
        return tasks, dropped
    return tasks, []


def gfm_axis_length(archive: str, storage_options: dict | None) -> int:
    """Length of the gfm ``time`` axis, or 0 if the group does not exist yet."""
    store = store_for(archive, "datacube.zarr", storage_options)
    try:
        group = datacube.open_root(store, mode="r")["gfm"]
        return int(group["time"].shape[0])
    except Exception:
        return 0


def validate_cube(archive: str, tasks: list[dict], storage_options: dict | None, axis_before: int) -> None:
    """Assert every task date is on the gfm time axis and the axis is sorted."""
    store = store_for(archive, "datacube.zarr", storage_options)
    group = datacube.open_root(store, mode="r")["gfm"]
    units = group["time"].attrs.get("units", "days since 2020-01-01")
    epoch = str(units).rsplit("since ", 1)[-1].strip()
    times = np.asarray(group["time"][:], dtype="int64")
    task_ints = {datacube.date_to_int(_to_date(t["date"]), epoch) for t in tasks}
    missing = sorted(task_ints - set(times.tolist()))
    spans = unsorted_spans(times)
    print(f"gfm time axis: {axis_before} → {len(times)} entries (366 = full year)")
    if missing:
        raise SystemExit(f"Validation failed: {len(missing)} task date(s) missing from the time axis: {missing[:10]}")
    if spans:
        raise SystemExit(f"Validation failed: time axis not ascending at {spans}")
    print("Validation OK: all task dates present on the axis, axis ascending.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--archive", default="s3://atlantis/zarr/geoidflood_events", help="cube root (parent of datacube.zarr)"
    )
    parser.add_argument(
        "--year",
        type=int,
        default=0,
        help="YYYY: backfill into s3://atlantis/zarr/YYYY (overrides --archive); tasks are filtered to that year",
    )
    parser.add_argument(
        "--events",
        default="",
        help="comma-separated event ids (or event-aoi ids, e.g. EMSR712-10) to backfill",
    )
    parser.add_argument(
        "--db-path", type=Path, default=Path("geoidflood_gfm_cube_tracker.db"), help="SQLite resume tracker"
    )
    parser.add_argument("--workers", type=int, nargs=2, default=[2, 6], help="Dask workers min max")
    parser.add_argument("--memory-limit", default="4GB", help="memory cap per worker")
    parser.add_argument("--tasks-only", action="store_true", help="only generate the task list, do not run the batch")
    parser.add_argument("--tasks", type=Path, default=TASKS_ALL_PATH, help="task list (built if missing)")
    args = parser.parse_args()

    events = {e.strip() for e in args.events.split(",") if e.strip()}
    year = args.year or None

    aoi_table = pd.read_csv(AOI_TABLE)
    aoi_table = filter_aoi_rows(aoi_table, events)
    if year:
        # Rows with no overlap with the backfill year contribute no tasks for
        # it; dropping them up front also avoids pointless live STAC searches
        # for windows entirely outside the year.
        aoi_table = aoi_table[
            (aoi_table["date_start"] <= f"{year}-12-31") & (aoi_table["date_end"] >= f"{year}-01-01")
        ]

    tasks, dropped = load_or_build_tasks(args.tasks, aoi_table)
    tasks = filter_tasks(tasks, events, year)
    if not tasks:
        print("No tasks match the requested event/year filter — nothing to do.")
        return
    print(f"Tasks: {len(tasks):,} ({sum(len(t['item_hrefs']) for t in tasks):,} items)")

    if args.tasks_only:
        if events or year:
            out = filtered_tasks_path(args.tasks, events, year)
            write_tasks(out, tasks, dropped)
            print(f"Filtered task list: {out}")
        return

    archive = f"s3://atlantis/zarr/{year}" if year else args.archive

    storage_options = None
    if archive.startswith("s3://"):
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

    axis_before = gfm_axis_length(archive, storage_options)
    t0 = time.monotonic()
    final = run_gfm_cube_batch(
        tasks,
        archive_root=archive,
        cfg=cfg,
        storage_options=storage_options,
        ordered=True,
        prefill_year=year,
    )
    print(
        f"DONE={final.get('DONE', 0)} FAILED={final.get('FAILED', 0)} of {len(tasks)} "
        f"in {(time.monotonic() - t0) / 3600:.1f}h → {archive}"
    )
    validate_cube(archive, tasks, storage_options, axis_before)


if __name__ == "__main__":
    main()
