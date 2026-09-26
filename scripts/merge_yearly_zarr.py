"""Merge the yearly Zarr datacubes into one multi-year store, then rebuild its STAC catalog.

Every yearly store (``<src-root>/<YYYY>/datacube.zarr``) shares the same grid,
codecs, chunking, sharding and time epoch (the "config-identity guarantee" in
``docs/archive/zarr-spec.md``). Data arrays are sharded ``(1, 2048, 2048)``, so
each stored object holds exactly one time slot: merging is a **server-side
copy** of every shard object with its time index rewritten
(``<var>/c/<t_old>/<sy>/<sx>`` → ``<var>/c/<t_new>/<sy>/<sx>``) — no data is
decoded or re-encoded. Only the ``time`` axis changes.

The script refuses to merge (before writing anything) unless, per source group:

* every array's ``zarr.json`` is identical across years except ``shape``;
* every recorded ``archive_config`` fingerprint is identical;
* every time slot falls inside its store's year — empty out-of-year slots
  (legacy 366-slot prefills of 365-day years) are dropped, populated ones fail;
* no date appears twice.

Group attributes are merged: ``atlantis_time_prefill`` (a single-year marker) is
dropped and replaced by ``atlantis_merged_years``; ``atlantis_events`` bookmarks
are unioned. Variables that hold no data in any year are omitted.

By default every source gets a **contiguous** time axis covering every day of
the ``--years`` span (1 Jan of the first year → 31 Dec of the last), so any day
— including gaps no yearly store covers — has a pre-existing slot that later
backfills region-write into. Unwritten days read NODATA (255). ``--sparse``
keeps only the days present in some yearly store instead.

Re-runnable: a shard is skipped when the destination already holds an object
with the same size and ETag (server-side copies preserve the ETag), so re-running
after the yearly stores change re-copies only the modified shards. A merged
axis that only grows at its end (e.g. ``--years 2016-2026`` after 2016-2025)
is extended in place — existing slots keep their index, so only the new shards
are copied. Any other axis change stops the run unless ``--rebuild-changed`` is
given, which deletes and rebuilds that merged group.

Run::

    pixi run merge-yearly-zarr --dry-run      # inspect + plan, no writes
    pixi run merge-yearly-zarr                # merge + consolidate + STAC

Writes only under ``--dest``; the yearly stores are read-only inputs.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import fsspec
import numpy as np
import zarr
from fsspec.asyn import _run_coros_in_chunks, sync
from fsspec.implementations.local import LocalFileSystem
from loguru import logger

from atlantis.archive._store import is_remote, store_for
from atlantis.archive.reindex_time import _consolidate_verified
from atlantis.batch.catalog import DEFAULT_S3_ENDPOINT

_STORE = "datacube.zarr"
_CONFIG_ATTR = "archive_config"
_EVENTS_ATTR = "atlantis_events"
_PREFILL_ATTR = "atlantis_time_prefill"
_MERGED_ATTR = "atlantis_merged_years"
_COORDS = ("time", "y", "x")

#: ``(size, token)`` of a stored object; local files use a content digest.
Obj = tuple[int, str | None]


class MergeError(RuntimeError):
    """The yearly stores cannot be merged as-is (nothing has been written)."""


@dataclass
class YearGroup:
    """One source group of one yearly store."""

    year: int
    source: str
    root: str
    path: str
    attrs: dict[str, Any]
    arrays: dict[str, dict[str, Any]]
    times: np.ndarray
    chunks: dict[str, dict[str, Obj]]


@dataclass
class SourcePlan:
    """Everything needed to write one merged source group."""

    source: str
    groups: list[YearGroup]
    axis: np.ndarray
    var_names: list[str]
    arrays: dict[str, dict[str, Any]]
    attrs: dict[str, Any]
    index_maps: dict[int, dict[int, int]]
    dropped_slots: list[tuple[int, date]] = field(default_factory=list)
    empty_vars: list[str] = field(default_factory=list)


@dataclass
class CopyPlan:
    """Pending copies for one merged source group."""

    pairs: list[tuple[str, str]]
    expected: dict[str, dict[str, Obj]]
    up_to_date: int
    stale: int


# ── paths / filesystem ──────────────────────────────────────────────────────


def _fs_path(root: str, *parts: str) -> str:
    """Filesystem-native path (no scheme) of *parts* under *root*."""
    base = str(root).split("://", 1)[1] if is_remote(root) else str(Path(root).resolve())
    return "/".join([base.rstrip("/"), *parts])


def _filesystem(root: str, storage_options: dict[str, Any] | None) -> fsspec.AbstractFileSystem:
    if is_remote(root):
        fs, _ = fsspec.url_to_fs(root, **(storage_options or {}))
        return fs
    return fsspec.filesystem("file", auto_mkdir=True)


def _object_token(fs: fsspec.AbstractFileSystem, path: str, info: dict[str, Any]) -> str | None:
    if token := info.get("ETag"):
        return str(token)
    if isinstance(fs, LocalFileSystem):
        digest = hashlib.blake2b(digest_size=16)
        with fs.open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    return None


def _list_files(fs: fsspec.AbstractFileSystem, array_dir: str) -> dict[str, Obj]:
    """Stored objects of an array keyed by path relative to the array, ``zarr.json`` excluded."""
    out: dict[str, Obj] = {}
    # Trailing slash: never match sibling arrays sharing a name prefix.
    for path, info in fs.find(array_dir + "/", detail=True).items():
        rel = path[len(array_dir) + 1 :]
        if rel and rel != "zarr.json":
            out[rel] = (int(info["size"]), _object_token(fs, path, info))
    return out


def _read_json(fs: fsspec.AbstractFileSystem, path: str) -> dict[str, Any]:
    return json.loads(fs.cat_file(path))


def _write_json(fs: fsspec.AbstractFileSystem, path: str, doc: dict[str, Any]) -> None:
    fs.pipe_file(path, json.dumps(doc, indent=2).encode())


def _listing_delay(fs: fsspec.AbstractFileSystem) -> float:
    """Seconds to wait before re-listing: object-store listings can lag writes."""
    return 0.0 if isinstance(fs, LocalFileSystem) else 5.0


def _rm_tree(fs: fsspec.AbstractFileSystem, path: str) -> None:
    """Delete *path* recursively and wait until listings confirm it is gone."""
    for _ in range(12):
        if not fs.find(path + "/"):
            return
        fs.rm(path, recursive=True)
        fs.invalidate_cache()
        time.sleep(_listing_delay(fs))
    raise RuntimeError(f"could not delete {path!r}")


def _copy_batch(fs: fsspec.AbstractFileSystem, pairs: list[tuple[str, str]], concurrency: int) -> None:
    """Copy ``(src, dst)`` pairs; on S3 each is a single server-side ``CopyObject``."""
    if getattr(fs, "async_impl", False) and hasattr(fs, "_copy_basic"):
        # _copy_basic skips the per-object HEAD that fs.copy/_cp_file issues first.
        coros = [fs._copy_basic(src, dst) for src, dst in pairs]
        results = sync(
            fs.loop, _run_coros_in_chunks, coros, batch_size=concurrency, nofiles=True, return_exceptions=True
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise errors[0]
    else:
        fs.copy([s for s, _ in pairs], [d for _, d in pairs])


# ── inspection ──────────────────────────────────────────────────────────────


def _epoch_of(units: str) -> date:
    return date.fromisoformat(str(units).rsplit("since ", 1)[-1].strip())


def _open_group(root: str, source: str, storage_options: dict[str, Any] | None, mode: str = "r") -> zarr.Group:
    """Open a source group from its own on-disk ``zarr.json``.

    ``root[source]`` would read the group's nested consolidated-metadata block,
    which can be stale (e.g. array shapes from before the last resize).
    """
    return zarr.open_group(store_for(root, _STORE, storage_options), path=source, mode=mode, use_consolidated=False)


def load_year_groups(
    fs: fsspec.AbstractFileSystem,
    src_root: str,
    years: list[int],
    sources: set[str] | None,
    storage_options: dict[str, Any] | None,
) -> dict[str, list[YearGroup]]:
    """Read every yearly group's on-disk metadata, time axis and chunk listing (read-only)."""
    by_source: dict[str, list[YearGroup]] = defaultdict(list)
    for year in years:
        year_root = f"{src_root.rstrip('/')}/{year}"
        store_path = _fs_path(year_root, _STORE)
        if not fs.exists(f"{store_path}/zarr.json"):
            logger.warning(f"{year}: no {_STORE} under {year_root} — skipped")
            continue
        root = zarr.open_group(store_for(year_root, _STORE, storage_options), mode="r", use_consolidated=False)
        for source in sorted(root.group_keys()):
            if sources and source not in sources:
                continue
            group = _open_group(year_root, source, storage_options)
            gpath = f"{store_path}/{source}"
            arrays = {name: _read_json(fs, f"{gpath}/{name}/zarr.json") for name in sorted(group.array_keys())}
            missing = [c for c in _COORDS if c not in arrays]
            if missing:
                raise MergeError(f"{year}/{source}: missing coordinate arrays {missing}")
            chunks = {name: _list_files(fs, f"{gpath}/{name}") for name, m in arrays.items() if len(m["shape"]) == 3}
            by_source[source].append(
                YearGroup(
                    year=year,
                    source=source,
                    root=year_root,
                    path=gpath,
                    attrs=dict(group.attrs),
                    arrays=arrays,
                    times=np.asarray(group["time"][:], dtype="int64"),
                    chunks=chunks,
                )
            )
            logger.info(
                f"{year}/{source}: {group['time'].shape[0]} slots, "
                f"{sum(len(c) for c in chunks.values())} shard objects in {len(chunks)} variables"
            )
    return dict(by_source)


def _without_shape(meta: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in meta.items() if k != "shape"}


def _merge_events(source: str, groups: list[YearGroup]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for g in groups:
        for name, rec in (g.attrs.get(_EVENTS_ATTR) or {}).items():
            if name not in merged:
                merged[name] = copy.deepcopy(rec)
                continue
            prev = merged[name]
            if prev.get("bbox") != rec.get("bbox"):
                raise MergeError(f"{source}: event bookmark {name!r} has different bboxes across years")
            prev["dates"] = sorted(set(prev.get("dates", [])) | set(rec.get("dates", [])))
            prev["updated_at"] = max(str(prev.get("updated_at", "")), str(rec.get("updated_at", "")))
    return merged


def plan_source(source: str, groups: list[YearGroup], span: tuple[date, date] | None = None) -> SourcePlan:
    """Validate that *groups* are mergeable and compute the merged layout.

    *span* ``(first, last)`` makes the axis every day in that inclusive range;
    ``None`` keeps only the days present in some yearly store.
    """
    groups = sorted(groups, key=lambda g: g.year)

    arrays: dict[str, dict[str, Any]] = {}
    owner: dict[str, int] = {}
    for g in groups:
        for name, meta in g.arrays.items():
            if name not in arrays:
                arrays[name], owner[name] = meta, g.year
                continue
            ref, cur = _without_shape(arrays[name]), _without_shape(meta)
            if ref != cur:
                keys = sorted(k for k in set(ref) | set(cur) if ref.get(k) != cur.get(k))
                raise MergeError(f"{source}/{name}: zarr.json of {g.year} differs from {owner[name]} in {keys}")
            spatial_ref, spatial_cur = arrays[name]["shape"][1:], meta["shape"][1:]
            if spatial_ref != spatial_cur:
                raise MergeError(f"{source}/{name}: non-time shape of {g.year} differs from {owner[name]}")

    recorded = {g.year: g.attrs[_CONFIG_ATTR] for g in groups if _CONFIG_ATTR in g.attrs}
    if not recorded:
        raise MergeError(f"{source}: no year records an {_CONFIG_ATTR!r} fingerprint")
    if len({json.dumps(v, sort_keys=True) for v in recorded.values()}) > 1:
        raise MergeError(f"{source}: {_CONFIG_ATTR} differs across years: {recorded}")
    config = next(iter(recorded.values()))
    units = arrays["time"]["attributes"]["units"]
    if config.get("time_units") != units:
        raise MergeError(f"{source}: {_CONFIG_ATTR} time_units {config.get('time_units')!r} != axis units {units!r}")
    epoch = _epoch_of(units)

    slot_of: dict[int, tuple[int, int]] = {}
    dropped: list[tuple[int, date]] = []
    for g in groups:
        used = {int(rel.split("/")[1]) for files in g.chunks.values() for rel in files}
        beyond = sorted(t for t in used if t >= len(g.times))
        if beyond:
            raise MergeError(f"{source} {g.year}: shard objects beyond the time axis at slots {beyond[:5]}")
        seen: set[int] = set()
        for i, t in enumerate(g.times.tolist()):
            d = epoch + timedelta(days=int(t))
            if d.year != g.year:
                if i in used:
                    raise MergeError(f"{source} {g.year}: slot {i} ({d}) is outside the year but holds data")
                dropped.append((g.year, d))
                continue
            if t in seen:
                raise MergeError(f"{source} {g.year}: date {d} appears twice on the time axis")
            seen.add(t)
            slot_of[t] = (g.year, i)

    if span is None:
        axis = np.asarray(sorted(slot_of), dtype="int64")
    else:
        lo, hi = ((d - epoch).days for d in span)
        outside = sorted(epoch + timedelta(days=t) for t in slot_of if not lo <= t <= hi)
        if outside:
            raise MergeError(f"{source}: dates outside the merge span {span}: {outside[:5]}")
        axis = np.arange(lo, hi + 1, dtype="int64")
    position = {t: i for i, t in enumerate(axis.tolist())}
    index_maps: dict[int, dict[int, int]] = defaultdict(dict)
    for t, (year, old_i) in slot_of.items():
        index_maps[year][old_i] = position[t]

    data_vars = sorted(n for n, m in arrays.items() if len(m["shape"]) == 3)
    var_names = [n for n in data_vars if any(g.chunks.get(n) for g in groups)]
    empty_vars = [n for n in data_vars if n not in var_names]

    ref = groups[-1]
    attrs = dict(ref.attrs)
    attrs.pop(_PREFILL_ATTR, None)
    attrs.update(
        {
            "source_id": source,
            _CONFIG_ATTR: config,
            _EVENTS_ATTR: _merge_events(source, groups),
            _MERGED_ATTR: [g.year for g in groups],
        }
    )
    last = [str(g.attrs["last_updated"]) for g in groups if g.attrs.get("last_updated")]
    if last:
        attrs["last_updated"] = max(last)

    n = len(axis)
    merged_arrays: dict[str, dict[str, Any]] = {}
    for name in (*_COORDS, "crs", *var_names):
        if name not in arrays:
            continue
        meta = copy.deepcopy(arrays[name])
        if name == "time":
            meta["shape"] = [n]
        elif name in var_names:
            meta["shape"] = [n, *meta["shape"][1:]]
        merged_arrays[name] = meta

    return SourcePlan(
        source=source,
        groups=groups,
        axis=axis,
        var_names=var_names,
        arrays=merged_arrays,
        attrs=attrs,
        index_maps=dict(index_maps),
        dropped_slots=dropped,
        empty_vars=empty_vars,
    )


# ── destination ─────────────────────────────────────────────────────────────


def dest_axis(dest_root: str, source: str, storage_options: dict[str, Any] | None) -> np.ndarray | None:
    """The existing merged time axis of *source*, or ``None`` if the group is absent."""
    fs = _filesystem(dest_root, storage_options)
    if not fs.exists(_fs_path(dest_root, _STORE, source, "time", "zarr.json")):
        return None
    return np.asarray(_open_group(dest_root, source, storage_options)["time"][:], dtype="int64")


def axis_extends(existing: np.ndarray, new: np.ndarray) -> bool:
    """True when *new* keeps every *existing* slot at its index (identical or tail-appended)."""
    return len(existing) <= len(new) and np.array_equal(new[: len(existing)], existing)


def plan_copies(fs: fsspec.AbstractFileSystem, plan: SourcePlan, dest_group: str) -> CopyPlan:
    """Map every source object to its merged key and keep those not already up to date."""
    pairs: list[tuple[str, str]] = []
    expected: dict[str, dict[str, Obj]] = {}
    up_to_date = stale = 0

    ref = plan.groups[-1]
    for name in ("y", "x", "crs"):
        if name not in plan.arrays:
            continue
        src_files = _list_files(fs, f"{ref.path}/{name}")
        dst_files = _list_files(fs, f"{dest_group}/{name}")
        expected[name] = src_files
        for rel, obj in src_files.items():
            if dst_files.get(rel) == obj:
                up_to_date += 1
            else:
                pairs.append((f"{ref.path}/{name}/{rel}", f"{dest_group}/{name}/{rel}"))

    for var in plan.var_names:
        dst_files = _list_files(fs, f"{dest_group}/{var}")
        want: dict[str, Obj] = {}
        for g in plan.groups:
            mapping = plan.index_maps.get(g.year, {})
            for rel, obj in g.chunks.get(var, {}).items():
                parts = rel.split("/")
                new_rel = "/".join(["c", str(mapping[int(parts[1])]), *parts[2:]])
                want[new_rel] = obj
                if dst_files.get(new_rel) == obj:
                    up_to_date += 1
                else:
                    pairs.append((f"{g.path}/{var}/{rel}", f"{dest_group}/{var}/{new_rel}"))
        stale += len(set(dst_files) - set(want))
        expected[var] = want
    return CopyPlan(pairs=pairs, expected=expected, up_to_date=up_to_date, stale=stale)


def write_metadata(
    fs: fsspec.AbstractFileSystem,
    plan: SourcePlan,
    dest_root: str,
    storage_options: dict[str, Any] | None,
) -> None:
    """Write the merged group/array ``zarr.json`` documents and the time values."""
    store_path = _fs_path(dest_root, _STORE)
    # A plain root (no consolidated block) until the final consolidation, so a
    # crashed run is never read through stale consolidated metadata.
    _write_json(fs, f"{store_path}/zarr.json", {"zarr_format": 3, "node_type": "group", "attributes": {}})
    gpath = f"{store_path}/{plan.source}"
    _write_json(fs, f"{gpath}/zarr.json", {"zarr_format": 3, "node_type": "group", "attributes": plan.attrs})
    for name, meta in plan.arrays.items():
        _write_json(fs, f"{gpath}/{name}/zarr.json", meta)
    fs.invalidate_cache()
    if len(plan.axis):
        _open_group(dest_root, plan.source, storage_options, mode="r+")["time"][:] = plan.axis


def run_copies(fs: fsspec.AbstractFileSystem, pairs: list[tuple[str, str]], *, workers: int, batch: int) -> None:
    total = len(pairs)
    started = time.monotonic()
    for lo in range(0, total, batch):
        chunk = pairs[lo : lo + batch]
        for attempt in range(1, 6):
            try:
                _copy_batch(fs, chunk, workers)
                break
            except Exception as exc:  # noqa: BLE001 - copies are idempotent, retry the batch
                if attempt == 5:
                    raise
                logger.warning(f"copy batch at {lo} failed (attempt {attempt}): {exc!r} — retrying")
                time.sleep(5 * attempt)
        done = lo + len(chunk)
        rate = done / max(time.monotonic() - started, 1e-6)
        eta_min = (total - done) / rate / 60
        logger.info(f"copied {done}/{total} ({100 * done / total:.1f}%) · {rate:.0f} obj/s · ETA {eta_min:.1f} min")


def verify_copies(fs: fsspec.AbstractFileSystem, dest_group: str, expected: dict[str, dict[str, Obj]]) -> None:
    """Every expected object is present at the destination with the source's size/ETag."""
    for name, want in expected.items():
        missing: list[str] = []
        for _ in range(12):
            fs.invalidate_cache()
            got = _list_files(fs, f"{dest_group}/{name}")
            missing = [rel for rel, obj in want.items() if got.get(rel) != obj]
            if not missing:
                break
            time.sleep(_listing_delay(fs))
        if missing:
            raise RuntimeError(
                f"{dest_group}/{name}: {len(missing)} object(s) missing or different, e.g. {missing[:3]}"
            )
        logger.info(f"verified {dest_group}/{name}: {len(want)} object(s)")


def spot_check(
    plan: SourcePlan, dest_root: str, storage_options: dict[str, Any] | None, samples: int, seed: int = 0
) -> int:
    """Compare random shard regions bit-for-bit (raw uint8) between yearly and merged stores."""
    rng = random.Random(seed)
    dst = zarr.open_group(store_for(dest_root, _STORE, storage_options), mode="r")[plan.source]
    checked = 0
    for g in plan.groups:
        src = _open_group(g.root, plan.source, storage_options)
        candidates = [(var, rel) for var in plan.var_names for rel in g.chunks.get(var, {})]
        for var, rel in rng.sample(candidates, min(samples, len(candidates))):
            _, t, sy, sx = rel.split("/")
            ty, tx = plan.arrays[var]["chunk_grid"]["configuration"]["chunk_shape"][1:]
            rows = slice(int(sy) * ty, (int(sy) + 1) * ty)
            cols = slice(int(sx) * tx, (int(sx) + 1) * tx)
            a = np.asarray(src[var][int(t), rows, cols])
            b = np.asarray(dst[var][plan.index_maps[g.year][int(t)], rows, cols])
            if not np.array_equal(a, b):
                raise RuntimeError(f"{plan.source}/{var} {g.year} slot {t} shard ({sy},{sx}): merged data differs")
            checked += 1
    return checked


def build_stac(dest_root: str, *, compute_bbox: bool, sources: list[str] | None) -> str:
    """Rebuild ``<dest>/stac`` exactly as ``atlantis stac build`` does for the yearly stores."""
    from atlantis.config import get_config
    from atlantis.stac import build_datacube_catalog, write_catalog

    config = get_config()
    storage_options = config.archive.storage_options or None
    stac_config = config.stac.model_copy(update={"compute_item_bbox": compute_bbox})
    catalog = build_datacube_catalog(
        dest_root,
        sources=sources,
        storage_options=storage_options,
        archive_config=config.archive,
        stac_config=stac_config,
    )
    dest = f"{dest_root.rstrip('/')}/stac"
    n_items = sum(1 for _ in catalog.get_items(recursive=True))
    write_catalog(catalog, dest, storage_options=storage_options)
    logger.info(f"STAC catalog written → {dest} ({n_items} items)")
    return dest


# ── CLI ─────────────────────────────────────────────────────────────────────


def _parse_years(spec: str) -> list[int]:
    years: set[int] = set()
    for part in spec.split(","):
        lo, _, hi = part.strip().partition("-")
        years.update(range(int(lo), int(hi or lo) + 1))
    return sorted(years)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src-root", default="s3://atlantis/zarr", help="Parent of the <YYYY>/datacube.zarr stores.")
    ap.add_argument("--years", default="2016-2025", help="Years to merge, e.g. '2016-2025' or '2016,2018-2020'.")
    ap.add_argument("--dest", default="s3://atlantis/zarr/archive", help="Merged archive root (datacube.zarr + stac/).")
    ap.add_argument("--source", action="append", dest="sources", help="Limit to these source groups (repeatable).")
    ap.add_argument("--endpoint-url", default=DEFAULT_S3_ENDPOINT, help="S3 endpoint for s3:// roots.")
    ap.add_argument("--workers", type=int, default=64, help="Concurrent server-side copies.")
    ap.add_argument("--batch", type=int, default=5000, help="Copies per progress/retry batch.")
    ap.add_argument("--verify-samples", type=int, default=2, help="Shards compared bit-for-bit per source-year.")
    ap.add_argument("--dry-run", action="store_true", help="Inspect and plan only; write nothing.")
    ap.add_argument(
        "--sparse",
        action="store_true",
        help="Keep only days present in some yearly store (default: every day of the --years span).",
    )
    ap.add_argument(
        "--rebuild-changed",
        action="store_true",
        help="Delete + rebuild a merged group whose axis changed other than by appending at its end.",
    )
    ap.add_argument("--skip-merge", action="store_true", help="Skip the Zarr merge (only rebuild STAC).")
    ap.add_argument("--skip-stac", action="store_true", help="Skip the STAC rebuild.")
    ap.add_argument(
        "--no-compute-bbox",
        action="store_true",
        help="Use the source extent for every STAC item (the yearly catalogs computed per-date bboxes).",
    )
    return ap.parse_args(argv)


def _check_roots(src_root: str, dest: str, years: list[int]) -> None:
    if is_remote(src_root) != is_remote(dest):
        raise MergeError("--src-root and --dest must both be local or both be remote (server-side copy)")
    if re.search(r"(?:^|/)zarr/\d{4}/?$", dest):
        raise MergeError(f"--dest {dest!r} looks like a yearly root (zarr/<YYYY>); pick a non-year name")
    dest_norm = dest.rstrip("/")
    if any(dest_norm == f"{src_root.rstrip('/')}/{y}" for y in years):
        raise MergeError(f"--dest {dest!r} is one of the yearly input stores")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    years = _parse_years(args.years)
    storage_options = {"endpoint_url": args.endpoint_url} if is_remote(args.dest) else None
    _check_roots(args.src_root, args.dest, years)
    fs = _filesystem(args.dest, storage_options)

    if not args.skip_merge:
        groups = load_year_groups(fs, args.src_root, years, set(args.sources or []) or None, storage_options)
        if not groups:
            raise MergeError(f"no source groups found under {args.src_root} for years {years}")
        span = None if args.sparse else (date(years[0], 1, 1), date(years[-1], 12, 31))
        plans = [plan_source(source, gs, span) for source, gs in sorted(groups.items())]

        rebuild: set[str] = set()
        extend: set[str] = set()
        for plan in plans:
            existing = dest_axis(args.dest, plan.source, storage_options)
            if existing is None or np.array_equal(existing, plan.axis):
                continue
            if axis_extends(existing, plan.axis):
                extend.add(plan.source)
                continue
            if not args.rebuild_changed:
                raise MergeError(
                    f"{plan.source}: merged time axis changed other than by appending "
                    f"({len(existing)} → {len(plan.axis)} slots); "
                    "re-run with --rebuild-changed to delete and rebuild that group"
                )
            rebuild.add(plan.source)

        copy_plans: dict[str, CopyPlan] = {}
        for plan in plans:
            dest_group = _fs_path(args.dest, _STORE, plan.source)
            copy_plans[plan.source] = (
                CopyPlan(pairs=[], expected={}, up_to_date=0, stale=0)
                if plan.source in rebuild and args.dry_run
                else plan_copies(fs, plan, dest_group)
            )
            cp = copy_plans[plan.source]
            epoch = _epoch_of(plan.arrays["time"]["attributes"]["units"])
            first = epoch + timedelta(days=int(plan.axis[0])) if len(plan.axis) else None
            last = epoch + timedelta(days=int(plan.axis[-1])) if len(plan.axis) else None
            status = " · REBUILD" if plan.source in rebuild else " · EXTEND" if plan.source in extend else ""
            logger.info(
                f"[{plan.source}] years={[g.year for g in plan.groups]} slots={len(plan.axis)} ({first} … {last}) "
                f"vars={plan.var_names} events={len(plan.attrs[_EVENTS_ATTR])} · to copy={len(cp.pairs)} "
                f"up-to-date={cp.up_to_date} stale={cp.stale}{status}"
            )
            if plan.dropped_slots:
                logger.info(f"[{plan.source}] dropping empty out-of-year slots: {plan.dropped_slots}")
            if plan.empty_vars:
                logger.info(f"[{plan.source}] omitting variables with no data in any year: {plan.empty_vars}")
            if cp.stale:
                logger.warning(
                    f"[{plan.source}] {cp.stale} merged object(s) have no yearly counterpart (left in place)"
                )

        if args.dry_run:
            logger.info("dry run — nothing written")
            return 0

        for plan in plans:
            dest_group = _fs_path(args.dest, _STORE, plan.source)
            if plan.source in rebuild:
                logger.info(f"[{plan.source}] deleting changed merged group {dest_group}")
                _rm_tree(fs, dest_group)
                copy_plans[plan.source] = plan_copies(fs, plan, dest_group)
            write_metadata(fs, plan, args.dest, storage_options)
            run_copies(fs, copy_plans[plan.source].pairs, workers=args.workers, batch=args.batch)
            verify_copies(fs, dest_group, copy_plans[plan.source].expected)

        store = store_for(args.dest, _STORE, storage_options)
        for plan in plans:
            _consolidate_verified(store, plan.source, len(plan.axis))
        for plan in plans:
            n = spot_check(plan, args.dest, storage_options, args.verify_samples)
            logger.info(f"[{plan.source}] spot check passed ({n} shard(s) bit-identical)")

    if not args.skip_stac and not args.dry_run:
        build_stac(args.dest, compute_bbox=not args.no_compute_bbox, sources=args.sources)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MergeError as exc:
        logger.error(str(exc))
        sys.exit(2)
