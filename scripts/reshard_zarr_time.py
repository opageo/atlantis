"""Re-shard the local Zarr datacube along time for publication on the Hugging Face Hub.

The archive stores data arrays with one shard object per day, ``(1, 2048, 2048)``,
which yields ~1.45M files — far above the Hub's per-repo file recommendation and
~1,500 commits against its 128-commits/hour limit. This script writes a copy whose
data arrays use ``(T, 2048, 2048)`` shards (default ``T=32``), cutting the file
count ~T-fold. Inner chunks ``(1, 256, 256)``, codecs, fill values, dimension names
and attributes are unchanged, so readers get identical values and the same
chunk-level partial reads. Coordinate/CRS arrays are copied verbatim and the root
metadata is consolidated.

The copy is a read-only publication artefact: ``ArchiveWriter`` and
``merge_yearly_zarr.py`` assume one time slot per shard, so keep writing to the
original store and re-run this script before publishing.

Re-runnable: destination shards that already exist are skipped.

Run::

    python scripts/reshard_zarr_time.py --dry-run
    python scripts/reshard_zarr_time.py --workers 8 --verify 3
"""

from __future__ import annotations

import argparse
import copy
import json
import multiprocessing as mp
import os
import random
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
import zarr
from loguru import logger
from tqdm import tqdm

DEFAULT_SRC = Path("/mnt/data/s3/atlantis/zarr/archive/datacube.zarr")
DEFAULT_DST = Path("/mnt/data/s3/atlantis/zarr/archive_hf/datacube.zarr")
DEFAULT_TIME_SHARD = 32

#: ``(time block, shard row, shard col)`` — the chunk-grid key of one destination shard.
Block = tuple[int, int, int]
Shape3 = tuple[int, int, int]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _write_json(path: Path, meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2))


def _is_time_cube(meta: dict[str, Any]) -> bool:
    dims = meta.get("dimension_names") or []
    return meta.get("node_type") == "array" and len(meta["shape"]) == 3 and dims[:1] == ["time"]


def _copy_group_meta(src: Path, dst: Path) -> None:
    """Copy a group's ``zarr.json``, dropping stale consolidated metadata."""
    meta = _read_json(src) if src.is_file() else {"zarr_format": 3, "node_type": "group", "attributes": {}}
    meta.pop("consolidated_metadata", None)
    _write_json(dst, meta)


def _prepare_cube(src_dir: Path, dst_dir: Path, time_shard: int) -> Shape3:
    """Write the destination array metadata with a time-extended shard shape."""
    meta = _read_json(src_dir / "zarr.json")
    key_enc = meta["chunk_key_encoding"]
    if key_enc["name"] != "default" or key_enc.get("configuration", {}).get("separator", "/") != "/":
        raise SystemExit(f"{src_dir}: unsupported chunk_key_encoding {key_enc}")
    shard = meta["chunk_grid"]["configuration"]["chunk_shape"]
    if shard[0] != 1:
        raise SystemExit(f"{src_dir}: expected one time slot per shard, got shard shape {shard}")

    new = copy.deepcopy(meta)
    new["chunk_grid"]["configuration"]["chunk_shape"] = [time_shard, *shard[1:]]
    dst_meta = dst_dir / "zarr.json"
    if dst_meta.is_file() and _read_json(dst_meta) != new:
        raise SystemExit(f"{dst_meta} exists with different metadata — remove it or pick another --dst")
    _write_json(dst_meta, new)
    return (time_shard, shard[1], shard[2])


def _source_blocks(src_dir: Path, time_shard: int) -> tuple[set[Block], int]:
    """Destination blocks holding at least one populated source shard, plus the source shard count."""
    blocks: set[Block] = set()
    n_files = 0
    chunk_root = src_dir / "c"
    if not chunk_root.is_dir():
        return blocks, 0
    for t in os.scandir(chunk_root):
        for sy in os.scandir(t.path):
            for sx in os.scandir(sy.path):
                n_files += 1
                blocks.add((int(t.name) // time_shard, int(sy.name), int(sx.name)))
    return blocks, n_files


def _block_selection(block: Block, shard: Shape3) -> tuple[slice, slice, slice]:
    return tuple(slice(i * n, (i + 1) * n) for i, n in zip(block, shard))  # type: ignore[return-value]


def _dst_shard_path(dst_dir: Path, block: Block) -> Path:
    return dst_dir.joinpath("c", *map(str, block))


def _copy_block(src_dir: str, dst_dir: str, block: Block, shard: Shape3) -> None:
    src = zarr.open_array(src_dir, mode="r")
    dst = zarr.open_array(dst_dir, mode="r+")
    sel = _block_selection(block, shard)
    dst[sel] = src[sel]


def _verify(src_dir: Path, dst_dir: Path, blocks: list[Block], shard: Shape3, n: int) -> None:
    src = zarr.open_array(str(src_dir), mode="r")
    dst = zarr.open_array(str(dst_dir), mode="r")
    for block in random.sample(blocks, min(n, len(blocks))):
        sel = _block_selection(block, shard)
        if not np.array_equal(src[sel], dst[sel]):
            raise SystemExit(f"Verification failed for {dst_dir} block {block}")
    logger.info(f"Verified {min(n, len(blocks))} block(s) of {dst_dir.relative_to(dst_dir.parents[1])}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC, help=f"Source datacube.zarr (default: {DEFAULT_SRC})")
    parser.add_argument(
        "--dst", type=Path, default=DEFAULT_DST, help=f"Destination datacube.zarr (default: {DEFAULT_DST})"
    )
    parser.add_argument("--time-shard", type=int, default=DEFAULT_TIME_SHARD, help="Time slots per shard (default: 32)")
    parser.add_argument("--groups", nargs="+", help="Source groups to process (default: all)")
    parser.add_argument("--workers", type=int, default=4, help="Parallel worker processes (default: 4)")
    parser.add_argument(
        "--verify", type=int, default=0, metavar="N", help="Compare N random blocks per array after copying"
    )
    parser.add_argument("--dry-run", action="store_true", help="Only report the file-count reduction")
    args = parser.parse_args()

    src_root, dst_root = args.src.resolve(), args.dst.resolve()
    if src_root == dst_root:
        raise SystemExit("--dst must differ from --src")

    group_dirs = sorted(p for p in src_root.iterdir() if (p / "zarr.json").is_file())
    if args.groups:
        group_dirs = [p for p in group_dirs if p.name in args.groups]

    # (src array dir, dst array dir, shard shape, blocks)
    cubes: list[tuple[Path, Path, Shape3, list[Block]]] = []
    total_src = total_dst = 0
    for group_dir in group_dirs:
        if _read_json(group_dir / "zarr.json").get("node_type") != "group":
            continue
        dst_group = dst_root / group_dir.name
        if not args.dry_run:
            _copy_group_meta(group_dir / "zarr.json", dst_group / "zarr.json")

        for node in sorted(p for p in group_dir.iterdir() if (p / "zarr.json").is_file()):
            meta = _read_json(node / "zarr.json")
            if not _is_time_cube(meta):
                if not args.dry_run:
                    shutil.copytree(node, dst_group / node.name, dirs_exist_ok=True)
                continue

            blocks, n_src = _source_blocks(node, args.time_shard)
            total_src += n_src
            total_dst += len(blocks)
            logger.info(f"{group_dir.name}/{node.name}: {n_src:,} shard files -> {len(blocks):,}")
            if not args.dry_run:
                shard = _prepare_cube(node, dst_group / node.name, args.time_shard)
                cubes.append((node, dst_group / node.name, shard, sorted(blocks)))

    logger.info(f"Total data shards: {total_src:,} -> {total_dst:,} (time shard = {args.time_shard})")
    if args.dry_run:
        return

    tasks = [
        (src_dir, dst_dir, block, shard)
        for src_dir, dst_dir, shard, blocks in cubes
        for block in blocks
        if not _dst_shard_path(dst_dir, block).exists()
    ]
    logger.info(f"{len(tasks):,} shard(s) to write ({total_dst - len(tasks):,} already present)")

    # spawn: zarr runs a background event-loop thread that must not be forked.
    with ProcessPoolExecutor(max_workers=args.workers, mp_context=mp.get_context("spawn")) as pool:
        futures = [pool.submit(_copy_block, str(s), str(d), b, sh) for s, d, b, sh in tasks]
        for future in tqdm(as_completed(futures), total=len(futures), unit="shard"):
            future.result()

    _copy_group_meta(src_root / "zarr.json", dst_root / "zarr.json")
    zarr.consolidate_metadata(str(dst_root))
    logger.info(f"Consolidated metadata at {dst_root / 'zarr.json'}")

    if args.verify:
        for src_dir, dst_dir, shard, blocks in cubes:
            if blocks:
                _verify(src_dir, dst_dir, blocks, shard, args.verify)


if __name__ == "__main__":
    main()
