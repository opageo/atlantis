"""Validate the consolidated Zarr datacube against the deployed VIIRS COGs.

Pipeline (parallel-produce / serial-write — the only concurrency-safe way to
fill one Zarr cube):

  1. Load the VIIRS 2020 catalogue and take the first ``--limit`` granules.
  2. **Dask-parallel produce**: each worker downloads + classifies + harmonises
     a granule to a uint8 [0, 100] AOI array (``harmonise_granule_payload``).
  3. **Serial write**: a single coordinator region-writes every payload into the
     consolidated datacube (``ArchiveWriter.write_raw``). Writes are lock-free
     because only one process touches the cube's metadata / chunks.
  4. **Validate**:
       a. round-trip — read each AOI window back from the cube (undecoded uint8)
          and compare to the payload that was written;
       b. COG parity — compare the payload to the deployed COG at
          ``s3://atlantis/viirs/jpss/2020/<DATE>/GLB<NNN>.tif``.

Run::

    python scripts/validate_viirs_zarr.py --limit 100 --workers 6

The datacube is written locally (default ``data/_zarr_validation``); nothing is
written to S3.
"""

from __future__ import annotations

import argparse
import shutil
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

#: ECMWF object store endpoint (catalogue + COGs live here).
_S3_ENDPOINT = "https://object-store.os-api.cci1.ecmwf.int"


def _to_date(value) -> date:
    """Coerce a catalogue date (str / date / Timestamp / datetime64) to ``date``."""
    if isinstance(value, str):
        return date.fromisoformat(value)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", default="s3://atlantis/assets/viirs/viirs_2020_catalog.parquet")
    ap.add_argument("--limit", type=int, default=100, help="Number of leading catalogue rows to process.")
    ap.add_argument("--archive", default="data/_zarr_validation", help="Local datacube root.")
    ap.add_argument("--workers", type=int, default=6, help="Dask worker processes.")
    ap.add_argument("--memory-limit", default="4GB", help="Memory cap per Dask worker.")
    ap.add_argument("--no-compare-cogs", action="store_true", help="Skip the COG parity check.")
    ap.add_argument("--keep-archive", action="store_true", help="Do not delete an existing datacube first.")
    return ap.parse_args()


def _produce_payloads(tasks: list[dict], workers: int, memory_limit: str) -> tuple[list[dict], list[str]]:
    """Run the harmonise pipeline over *tasks* in parallel via a Dask cluster."""
    from dask.distributed import Client, LocalCluster, as_completed

    from atlantis.fetchers.viirs.batch_processor import harmonise_granule_payload

    payloads: list[dict] = []
    failures: list[str] = []
    cluster = LocalCluster(
        n_workers=workers,
        threads_per_worker=1,
        memory_limit=memory_limit,
        dashboard_address=":0",
    )
    with Client(cluster) as client:
        print(f"Dask dashboard: {client.dashboard_link}")
        futures = client.map(harmonise_granule_payload, tasks, retries=2, pure=False)
        for i, future in enumerate(as_completed(futures), start=1):
            try:
                payloads.append(future.result())
            except Exception as exc:  # noqa: BLE001 — record and continue
                failures.append(repr(exc))
            if i % 20 == 0 or i == len(tasks):
                print(f"  produced {i}/{len(tasks)} ({len(failures)} failed)")
    cluster.close()
    return payloads, failures


def _write_datacube(payloads: list[dict], archive_root: str, limit: int):
    """Region-write every payload into the consolidated datacube (serial)."""
    import xarray as xr

    from atlantis.archive._store import store_for
    from atlantis.archive.writer import ArchiveWriter
    from atlantis.config import ArchiveConfig
    from atlantis.models.event import FloodEvent

    west = float(min(p["x"].min() for p in payloads))
    east = float(max(p["x"].max() for p in payloads))
    south = float(min(p["y"].min() for p in payloads))
    north = float(max(p["y"].max() for p in payloads))
    dates = sorted({_to_date(p["date"]) for p in payloads})

    event = FloodEvent(
        event_id=f"viirs_2020_first{limit}",
        bbox=(west, south, east, north),
        start_date=dates[0],
        end_date=dates[-1],
        sources=["viirs"],
    )
    cfg = ArchiveConfig(archive_root=archive_root)
    writer = ArchiveWriter(archive_root, cfg)
    for p in payloads:
        ds = xr.Dataset(
            {"flood_fraction": (("y", "x"), p["scaled"])},
            coords={"y": p["y"], "x": p["x"]},
        )
        writer.write_raw(ds, event, "viirs", time=_to_date(p["date"]))
    return store_for(archive_root, cfg.raw_store, cfg.storage_options)


def _validate(payloads: list[dict], store, compare_cogs: bool) -> None:
    """Round-trip each AOI from the cube and (optionally) compare to its COG."""
    import xarray as xr
    from rasterio.io import MemoryFile

    from atlantis.archive import grid

    full = xr.open_zarr(store, group="viirs", mask_and_scale=False)
    times = full["time"].values.astype("datetime64[D]")

    fs = None
    if compare_cogs:
        import s3fs

        fs = s3fs.S3FileSystem(endpoint_url=_S3_ENDPOINT)

    rt_ok = rt_bad = 0
    cog_exact = cog_within1 = cog_off = cog_missing = cog_shape = 0
    global_max_abs = 0
    total_pixels = total_diff = 0
    examples: list[tuple] = []

    for p in payloads:
        scaled = p["scaled"]
        win = grid.coords_to_window(p["y"], p["x"])
        ti = int(np.flatnonzero(times == np.datetime64(_to_date(p["date"])))[0])
        sub = (
            full["flood_fraction"]
            .isel(
                time=ti,
                y=slice(win.row_start, win.row_stop),
                x=slice(win.col_start, win.col_stop),
            )
            .values
        )
        if sub.shape == scaled.shape and np.array_equal(sub, scaled):
            rt_ok += 1
        else:
            rt_bad += 1
            examples.append(("roundtrip", p["task_id"], sub.shape, scaled.shape))

        if fs is None:
            continue
        uri = f"s3://atlantis/{p['dest_key']}"
        try:
            with fs.open(uri, "rb") as f:
                cog_bytes = f.read()
            with MemoryFile(cog_bytes) as mem, mem.open() as src:
                cog = src.read(1)
        except FileNotFoundError:
            cog_missing += 1
            continue
        if cog.shape != scaled.shape:
            cog_shape += 1
            examples.append(("cog-shape", p["task_id"], cog.shape, scaled.shape))
            continue
        delta = cog.astype(np.int16) - scaled.astype(np.int16)
        nonzero = delta != 0
        n_diff = int(nonzero.sum())
        total_pixels += cog.size
        total_diff += n_diff
        if n_diff == 0:
            cog_exact += 1
        else:
            max_abs = int(np.abs(delta[nonzero]).max())
            global_max_abs = max(global_max_abs, max_abs)
            if max_abs <= 1:
                cog_within1 += 1
            else:
                cog_off += 1
                examples.append(("cog-off", p["task_id"], f"n={n_diff}", f"max|Δ|={max_abs}"))

    n = len(payloads)
    print("\n── Validation summary ─────────────────────────────────────────")
    print(f"  granules written        : {n}")
    print(f"  cube round-trip exact   : {rt_ok}/{n}  (mismatch: {rt_bad})")
    if compare_cogs:
        print(f"  COG identical           : {cog_exact}/{n}")
        print(f"  COG within +/-1 LSB     : {cog_within1}/{n}")
        print(f"  COG off by >1 LSB       : {cog_off}/{n}")
        print(f"  COG missing / shape     : {cog_missing} / {cog_shape}")
        if total_pixels:
            pct = 100.0 * total_diff / total_pixels
            print(f"  differing pixels        : {total_diff}/{total_pixels} ({pct:.4f}%)")
        print(f"  global max |delta| (LSB): {global_max_abs}  (1 LSB = 1% flood fraction)")
    if examples:
        print("\n  examples:")
        for example in examples[:10]:
            print("   ", example)

    print("\n  CUBE round-trip :", "PASS" if rt_bad == 0 else "FAIL")
    if compare_cogs:
        print(
            f"  COG parity      : {cog_exact} identical, {cog_within1} within +/-1 LSB, "
            f"{cog_off} differ >1 LSB, {cog_missing} missing"
        )
        print(
            "  Note: the cube stores the CURRENT pipeline output exactly (round-trip);\n"
            "        COG diffs reflect pipeline changes since the COGs were written\n"
            "        (reprojector fast-path / VIIRS cloud-fraction), not a cube defect."
        )


def main() -> None:
    args = _parse_args()

    from atlantis.fetchers.viirs.inventory import load_inventory, slice_partition, to_tasks

    print(f"Loading catalogue: {args.catalog}")
    df = load_inventory(args.catalog)
    df = slice_partition(df, f"0:{args.limit}")
    tasks = to_tasks(df)
    print(f"Processing first {len(tasks)} granule(s).")

    archive_dir = Path(args.archive)
    if archive_dir.exists() and not args.keep_archive:
        shutil.rmtree(archive_dir)

    payloads, failures = _produce_payloads(tasks, args.workers, args.memory_limit)
    print(f"Produced {len(payloads)} payload(s); {len(failures)} failure(s).")
    if failures:
        for f in failures[:5]:
            print("  produce failure:", f)
    if not payloads:
        raise SystemExit("No payloads produced — aborting.")

    store = _write_datacube(payloads, args.archive, args.limit)
    print(f"Datacube written: {store}")

    _validate(payloads, store, compare_cogs=not args.no_compare_cogs)


if __name__ == "__main__":
    main()
