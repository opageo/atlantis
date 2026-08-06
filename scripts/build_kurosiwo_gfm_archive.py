"""Build the KuroSiwo events-only GFM Zarr archive.

Generates one task per (event AOI, date) for the full KuroSiwo event set
(windows = metadata range + 14-day post-flood pad), then streams them
through the production GFM cube batch (:func:`run_gfm_cube_batch`) into a
new sparse Zarr cube — same schema/layout as the year cubes, so the existing
reader, STAC and viz tooling work unchanged. Resume-safe via a per-run
SQLite tracker: re-running skips DONE tasks.

Task items come from the per-year S3 catalogues where one exists
(2021–2022 for KuroSiwo), and from live EODC STAC searches otherwise.

Usage::

    PYTHONPATH=src pixi run -e batch python scripts/build_kurosiwo_gfm_archive.py \
        --archive s3://atlantis/zarr/kurosiwo_events \
        --db-path kurosiwo_gfm_cube_tracker.db

Run detached (tmux) — an SSH disconnect must not stop the coordinator.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from atlantis.fetchers.gfm.backend import GfmStacBackend  # noqa: E402
from atlantis.models.event import FloodEvent  # noqa: E402

AOI_TABLE = _REPO_ROOT / "data" / "metadata" / "kurosiwo_aois.csv"
TASKS_ALL_PATH = _REPO_ROOT / "data" / "benchmark" / "gfm_aoi_tasks_all.json"

#: Years with a published GFM catalogue on S3 (offline task building).
CATALOGUE_YEARS = {"2021", "2022", "2023", "2024", "2025"}


def build_tasks_from_catalogues(aoi_table: pd.DataFrame) -> tuple[list[dict], set[str]]:
    """Build (AOI, date) tasks for catalogue-covered years, offline."""
    from atlantis.fetchers.gfm.inventory import load_inventory

    tasks: list[dict] = []
    for year, rows in aoi_table.groupby(aoi_table["date_start"].str[:4]):
        if year not in CATALOGUE_YEARS:
            continue
        catalogue = load_inventory(f"s3://atlantis/assets/gfm/gfm_archive_catalog_{year}.parquet")
        for row in rows.itertuples(index=False):
            in_window = (catalogue["date"].astype(str) >= row.date_start) & (
                catalogue["date"].astype(str) <= row.date_end
            )
            intersects = (
                (catalogue["west"] < row.aoi_east)
                & (catalogue["east"] > row.aoi_west)
                & (catalogue["south"] < row.aoi_north)
                & (catalogue["north"] > row.aoi_south)
            )
            for (day, _tile), group in catalogue[in_window & intersects].groupby(["date", "equi7_tile"]):
                tasks.append(
                    {
                        "task_id": f"gfm-{row.aoi_id}-{str(day).replace('-', '')}",
                        "date": str(day),
                        "equi7_tile": row.aoi_id,
                        "item_hrefs": list(group["item_href"]),
                        "bbox": [float(row.aoi_west), float(row.aoi_south), float(row.aoi_east), float(row.aoi_north)],
                        "event_id": row.event_id,
                        "aoi_id": row.aoi_id,
                    }
                )
    return tasks, set(aoi_table.loc[~aoi_table["date_start"].str[:4].isin(CATALOGUE_YEARS), "event_id"])


def build_tasks_live(aoi_table: pd.DataFrame, events: set[str]) -> list[dict]:
    """Live STAC search per (AOI, date) for events in years without catalogues."""
    backend = GfmStacBackend()
    tasks: list[dict] = []
    for row in aoi_table[aoi_table["event_id"].isin(events)].itertuples(index=False):
        day = date.fromisoformat(row.date_start)
        while day <= date.fromisoformat(row.date_end):
            items = backend.search(
                FloodEvent(
                    event_id=row.event_id,
                    bbox=(row.aoi_west, row.aoi_south, row.aoi_east, row.aoi_north),
                    start_date=day,
                    end_date=day,
                )
            )
            if items:
                tasks.append(
                    {
                        "task_id": f"gfm-{row.aoi_id}-{day.isoformat().replace('-', '')}",
                        "date": day.isoformat(),
                        "equi7_tile": row.aoi_id,
                        "item_hrefs": [item.self_href for item in items],
                        "bbox": [float(row.aoi_west), float(row.aoi_south), float(row.aoi_east), float(row.aoi_north)],
                        "event_id": row.event_id,
                        "aoi_id": row.aoi_id,
                    }
                )
            day += timedelta(days=1)
        print(f"  {row.event_id} {row.aoi_id}: {sum(1 for t in tasks if t['aoi_id'] == row.aoi_id)} tasks")
    return tasks


def generate_tasks() -> list[dict]:
    """Full KuroSiwo task list: catalogues where available, live search otherwise."""
    aoi_table = pd.read_csv(AOI_TABLE)
    tasks, live_events = build_tasks_from_catalogues(aoi_table)
    if live_events:
        tasks += build_tasks_live(aoi_table, live_events)
    tasks.sort(key=lambda t: (t["event_id"], t["aoi_id"], t["date"]))
    return tasks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--archive", default="s3://atlantis/zarr/kurosiwo_events", help="cube root (parent of datacube.zarr)"
    )
    parser.add_argument(
        "--db-path", type=Path, default=Path("kurosiwo_gfm_cube_tracker.db"), help="SQLite resume tracker"
    )
    parser.add_argument("--workers", type=int, nargs=2, default=[2, 6], help="Dask workers min max")
    parser.add_argument("--memory-limit", default="4GB", help="memory cap per worker")
    parser.add_argument("--tasks-only", action="store_true", help="only generate the task list, do not run the batch")
    parser.add_argument("--tasks", type=Path, default=TASKS_ALL_PATH, help="task list (built if missing)")
    args = parser.parse_args()

    from atlantis.archive.cube_batch import run_gfm_cube_batch
    from atlantis.batch import BatchConfig
    from atlantis.utils.setup import AWS_PROFILES

    if not args.tasks.exists():
        print("Building task list (catalogues + live search)…")
        tasks = generate_tasks()
        args.tasks.parent.mkdir(parents=True, exist_ok=True)
        import json

        args.tasks.write_text(json.dumps(tasks, indent=1))
    else:
        import json

        tasks = json.loads(args.tasks.read_text())
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
        dashboard_port=8795,
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
