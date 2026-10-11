"""Build a static STAC catalog over the consolidated Zarr datacube.

Structure (Option A, adapted to the multi-group Atlantis cube)::

    Catalog (atlantis-datacube)
    └── Collection  (one per source group: gfm / modis / viirs)
        ├── datacube extension  (cube:dimensions + cube:variables)
        ├── asset "zarr"        (the datacube store, xarray-assets ext)
        └── Item                (one per populated date)
            ├── datacube extension  (single-time slice)
            └── asset "zarr"        (same store, xarray:open_kwargs group=source)

The catalog is the *discovery / metadata* layer ("what to load"); the Zarr store
remains the *data* layer ("how to load efficiently"). Each item references the
shared store with the xarray-assets ``xarray:open_kwargs`` so it can be opened
directly (e.g. via xpystac) and sliced to the item's ``datetime``.

See :mod:`atlantis.archive` for the cube schema and grid.
"""

from __future__ import annotations

import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Iterable

import fsspec
import numpy as np
import pystac
from loguru import logger
from pystac.extensions.datacube import DatacubeExtension, Dimension, Variable

from atlantis.archive import grid
from atlantis.archive._store import is_remote, store_for
from atlantis.archive.reader import ArchiveReader
from atlantis.config import ArchiveConfig, StacConfig

__all__ = [
    "BuildProgress",
    "build_datacube_catalog",
    "build_source_collection",
    "write_catalog",
]

#: xarray-assets extension — lets clients open the Zarr asset directly as xarray.
XARRAY_ASSETS_EXT = "https://stac-extensions.github.io/xarray-assets/v1.0.0/schema.json"
#: datacube extension schema (used by the manual fallback in :func:`_apply_datacube`).
DATACUBE_EXT = "https://stac-extensions.github.io/datacube/v2.2.0/schema.json"

_RES = grid.GLOBAL_RESOLUTION
#: Known physical units for cube variables (others are categorical / unitless).
_VAR_UNITS: dict[str, str] = {"water_fraction": "1"}
#: Global fallback bbox (west, south, east, north) when an extent cannot be computed.
_GLOBAL_BBOX = (grid.ORIGIN_LON, grid.ORIGIN_LAT - 180.0, grid.ORIGIN_LON + 360.0, grid.ORIGIN_LAT)

BBox = tuple[float, float, float, float]
#: Half-open ``(r0, r1, c0, c1)`` pixel window on the global grid.
Window = tuple[int, int, int, int]


@dataclass
class BuildProgress:
    """UI-agnostic progress callbacks for catalog building.

    Each field is an optional callable invoked while
    :func:`build_datacube_catalog` runs; unset callbacks are ignored. This lets a
    caller (e.g. the CLI) render progress without the builder depending on any UI
    library.

    Attributes:
        on_sources: Resolved source-group list, once up front.
        on_source_start: A source's build begins (before the populated-window scan).
        on_source_total: The item (date) count for a source becomes known.
        on_item: One item finished — advance by one.
        on_source_done: A source finished; carries its item count (0 if skipped).
    """

    on_sources: Callable[[list[str]], None] | None = None
    on_source_start: Callable[[str], None] | None = None
    on_source_total: Callable[[str, int], None] | None = None
    on_item: Callable[[str], None] | None = None
    on_source_done: Callable[[str, int], None] | None = None


def _emit(fn: Callable[..., None] | None, *args: Any) -> None:
    """Invoke an optional progress callback, ignoring it when unset."""
    if fn is not None:
        fn(*args)


# ── href / geometry helpers ────────────────────────────────────────────────


def _store_href(archive_root: str, store_name: str) -> str:
    """Resolve the Zarr store location (absolute local path or ``s3://`` URL)."""
    root = str(archive_root)
    if "://" in root and not root.startswith("file://"):
        return root.rstrip("/") + "/" + store_name
    if root.startswith("file://"):
        root = root[len("file://") :]
    return str(Path(root, store_name).resolve())


def _bbox_geometry(bbox: BBox) -> dict[str, Any]:
    """Return a GeoJSON Polygon (CCW exterior ring) for *bbox*."""
    w, s, e, n = bbox
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def _iso(d: date) -> str:
    """ISO-8601 UTC midnight timestamp string for a date (datacube extent value)."""
    return f"{d.isoformat()}T00:00:00Z"


def _dt(d: date) -> datetime:
    """UTC midnight :class:`datetime` for a date (STAC item datetime / extent)."""
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


# ── datacube extension helpers ─────────────────────────────────────────────


def _datacube_dimensions(bbox: BBox, t_start: str, t_end: str) -> dict[str, Dimension]:
    """Build ``cube:dimensions`` (x, y spatial + time) for *bbox* and time span."""
    w, s, e, n = bbox
    return {
        "x": Dimension.from_dict(
            {
                "type": "spatial",
                "axis": "x",
                "extent": [w, e],
                "reference_system": 4326,
                "step": _RES,
            }
        ),
        "y": Dimension.from_dict(
            {
                "type": "spatial",
                "axis": "y",
                "extent": [s, n],
                "reference_system": 4326,
                "step": -_RES,
            }
        ),
        "time": Dimension.from_dict({"type": "temporal", "extent": [t_start, t_end]}),
    }


def _datacube_variables(var_names: Iterable[str]) -> dict[str, Variable]:
    """Build ``cube:variables`` entries for the cube's data variables."""
    out: dict[str, Variable] = {}
    for name in var_names:
        props: dict[str, Any] = {"dimensions": ["time", "y", "x"], "type": "data"}
        if name in _VAR_UNITS:
            props["unit"] = _VAR_UNITS[name]
        out[name] = Variable.from_dict(props)
    return out


def _apply_datacube(
    obj: "pystac.Collection | pystac.Item",
    dimensions: dict[str, Dimension],
    variables: dict[str, Variable],
) -> None:
    """Apply the datacube extension, falling back to raw fields if unsupported.

    pystac's :class:`DatacubeExtension` supports both Collection and Item, but
    the fallback keeps this robust across pystac versions and object types.
    """
    try:
        dc = DatacubeExtension.ext(obj, add_if_missing=True)
        dc.apply(dimensions=dimensions, variables=variables)
        return
    except Exception:  # pragma: no cover - version/typing fallback
        if DATACUBE_EXT not in obj.stac_extensions:
            obj.stac_extensions.append(DATACUBE_EXT)
        target = obj.properties if isinstance(obj, pystac.Item) else obj.extra_fields
        target["cube:dimensions"] = {k: v.to_dict() for k, v in dimensions.items()}
        target["cube:variables"] = {k: v.to_dict() for k, v in variables.items()}


def _zarr_asset(
    store_href: str,
    source_id: str,
    *,
    media_type: str,
    title: str,
    storage_options: dict[str, Any] | None = None,
) -> pystac.Asset:
    """Build the Zarr data asset with xarray-assets ``open_kwargs``."""
    open_kwargs = {"engine": "zarr", "group": source_id, "consolidated": True, "decode_coords": "all"}
    extra: dict[str, Any] = {"xarray:open_kwargs": open_kwargs}
    if storage_options:
        extra["xarray:storage_options"] = storage_options
    return pystac.Asset(
        href=store_href,
        media_type=media_type,
        roles=["data"],
        title=title,
        extra_fields=extra,
    )


# ── populated-extent computation ───────────────────────────────────────────


def _window_bbox(win: Window, yvals: np.ndarray, xvals: np.ndarray) -> BBox:
    """Edge bbox of a pixel window (pixel centres ± half-resolution)."""
    r0, r1, c0, c1 = win
    north = float(yvals[r0]) + _RES / 2.0  # y descends north→south
    south = float(yvals[r1 - 1]) - _RES / 2.0
    west = float(xvals[c0]) - _RES / 2.0
    east = float(xvals[c1 - 1]) + _RES / 2.0
    return (west, south, east, north)


def _union(a: Window | None, b: Window) -> Window:
    if a is None:
        return b
    return (min(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), max(a[3], b[3]))


def _stored_blocks(reader: ArchiveReader, source_id: str, var: str) -> dict[int, list[tuple[int, int]]]:
    """Stored ``(row, col)`` chunk-grid blocks of *var* per time slot, from the object listing."""
    href = _store_href(reader.archive_root, reader.config.store)
    if is_remote(reader.archive_root):
        fs, base = fsspec.url_to_fs(href, **(reader.storage_options or {}))
    else:
        fs, base = fsspec.filesystem("file"), href
    prefix = f"{base.rstrip('/')}/{source_id}/{var}/c/"
    blocks: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for path in fs.find(prefix):
        t, row, col = path[len(prefix) :].split("/")
        blocks[int(t)].append((int(row), int(col)))
    return dict(blocks)


def _slot_windows(reader: ArchiveReader, source_id: str, var: str) -> dict[int, Window]:
    """Populated pixel window of *var* per time slot (slots without data are absent).

    Only slots holding stored blocks are read, each over its blocks' bounding
    region, so the cost follows the data present — not axis length × global grid.
    """
    import zarr

    store = store_for(reader.archive_root, reader.config.store, reader.storage_options or None)
    arr = zarr.open_group(store, path=source_id, mode="r", use_consolidated=False)[var]
    bh, bw = (arr.shards or arr.chunks)[1:]
    n_time, height, width = arr.shape
    fill = arr.attrs.get("_FillValue", arr.fill_value)
    slots = sorted(_stored_blocks(reader, source_id, var).items())
    logger.info(f"{source_id}: scanning extents of {len(slots)} populated slot(s)")
    started = monotonic()
    out: dict[int, Window] = {}
    for done, (t, blocks) in enumerate(slots, 1):
        if done % 100 == 0:
            rate = done / max(monotonic() - started, 1e-6)
            eta_min = (len(slots) - done) / rate / 60
            logger.info(f"{source_id}: scanned {done}/{len(slots)} slots · ETA {eta_min:.1f} min")
        if t >= n_time:
            continue
        rows = [b[0] for b in blocks]
        cols = [b[1] for b in blocks]
        r0, c0 = min(rows) * bh, min(cols) * bw
        r1, c1 = min((max(rows) + 1) * bh, height), min((max(cols) + 1) * bw, width)
        raw = np.asarray(arr[t, r0:r1, c0:c1])
        valid = raw != fill
        if raw.dtype.kind == "f":
            valid &= ~np.isnan(raw)
        if not valid.any():
            continue
        rr = np.flatnonzero(valid.any(axis=1))
        cc = np.flatnonzero(valid.any(axis=0))
        out[t] = (r0 + int(rr[0]), r0 + int(rr[-1]) + 1, c0 + int(cc[0]), c0 + int(cc[-1]) + 1)
    return out


# ── item / collection builders ─────────────────────────────────────────────


def _build_date_item(
    source_id: str,
    d: date,
    store_href: str,
    *,
    bbox: BBox,
    config: StacConfig,
    var_names: list[str],
    storage_options: dict[str, Any] | None = None,
) -> pystac.Item:
    """Build the STAC Item for one populated ``(source, date)``."""
    item = pystac.Item(
        id=f"{source_id}-{d.isoformat()}",
        geometry=_bbox_geometry(bbox),
        bbox=list(bbox),
        datetime=_dt(d),
        properties={},
    )
    _apply_datacube(item, _datacube_dimensions(bbox, _iso(d), _iso(d)), _datacube_variables(var_names))
    item.add_asset(
        "zarr",
        _zarr_asset(
            store_href,
            source_id,
            media_type=config.zarr_media_type,
            title=f"{source_id} {d.isoformat()} flood datacube (Zarr)",
            storage_options=storage_options,
        ),
    )
    if XARRAY_ASSETS_EXT not in item.stac_extensions:
        item.stac_extensions.append(XARRAY_ASSETS_EXT)
    return item


def build_source_collection(
    reader: ArchiveReader,
    source_id: str,
    store_href: str,
    *,
    config: StacConfig | None = None,
    storage_options: dict[str, Any] | None = None,
    var: str = "water_fraction",
    progress: BuildProgress | None = None,
) -> pystac.Collection | None:
    """Build the STAC Collection (with per-date items) for one source group.

    Args:
        reader: Reader bound to the datacube archive root.
        source_id: Source group name (e.g. ``"viirs"``).
        store_href: Absolute href of the datacube store (local path or ``s3://``).
        config: STAC configuration (defaults to :class:`StacConfig`).
        storage_options: fsspec options recorded on the Zarr asset (remote roots).
        var: Variable used to compute populated extents.
        progress: Optional :class:`BuildProgress` callbacks for progress reporting.

    Returns:
        The populated Collection, or ``None`` if the source has no time steps.
    """
    config = config or StacConfig()
    prog = progress or BuildProgress()
    _emit(prog.on_source_start, source_id)
    try:
        ds = reader.read(source_id)
    except FileNotFoundError:
        logger.warning(f"Source '{source_id}' not present in the datacube — skipping.")
        _emit(prog.on_source_done, source_id, 0)
        return None
    if "time" not in ds.dims or int(ds.sizes.get("time", 0)) == 0:
        logger.warning(f"Source '{source_id}' has no populated time steps — skipping.")
        _emit(prog.on_source_done, source_id, 0)
        return None

    var_names = [str(v) for v in ds.data_vars if str(v) != "crs"]

    day_of = [np.datetime64(t, "D").astype(object) for t in ds["time"].values]
    date_windows: dict[date, Window] = {}
    if config.compute_item_bbox and var in ds:
        for t, win in _slot_windows(reader, source_id, var).items():
            date_windows[day_of[t]] = _union(date_windows.get(day_of[t]), win)
    yvals = ds["y"].values
    xvals = ds["x"].values
    window: Window | None = None
    for win in date_windows.values():
        window = _union(window, win)
    src_bbox: BBox = _window_bbox(window, yvals, xvals) if window is not None else _GLOBAL_BBOX

    dates = sorted(set(day_of))
    d_min, d_max = dates[0], dates[-1]
    _emit(prog.on_source_total, source_id, len(dates))

    collection = pystac.Collection(
        id=f"{config.catalog_id}-{source_id}",
        title=f"{source_id} flood datacube",
        description=(
            f"Atlantis {source_id} flood datacube on the canonical 1-arcmin global grid "
            f"(EPSG:4326). One item per populated date; data served from the shared Zarr store."
        ),
        extent=pystac.Extent(
            spatial=pystac.SpatialExtent([list(src_bbox)]),
            temporal=pystac.TemporalExtent([[_dt(d_min), _dt(d_max)]]),
        ),
    )
    _apply_datacube(
        collection,
        _datacube_dimensions(src_bbox, _iso(d_min), _iso(d_max)),
        _datacube_variables(var_names),
    )
    collection.add_asset(
        "zarr",
        _zarr_asset(
            store_href,
            source_id,
            media_type=config.zarr_media_type,
            title=f"{source_id} flood datacube (Zarr)",
            storage_options=storage_options,
        ),
    )
    if XARRAY_ASSETS_EXT not in collection.stac_extensions:
        collection.stac_extensions.append(XARRAY_ASSETS_EXT)

    for d in dates:
        collection.add_item(
            _build_date_item(
                source_id,
                d,
                store_href,
                bbox=_window_bbox(date_windows[d], yvals, xvals) if d in date_windows else src_bbox,
                config=config,
                var_names=var_names,
                storage_options=storage_options,
            )
        )
        _emit(prog.on_item, source_id)
    _emit(prog.on_source_done, source_id, len(dates))
    logger.info(f"Built collection '{collection.id}' — {len(dates)} item(s).")
    return collection


def build_datacube_catalog(
    archive_root: str | None = None,
    *,
    sources: Iterable[str] | None = None,
    storage_options: dict[str, Any] | None = None,
    archive_config: ArchiveConfig | None = None,
    stac_config: StacConfig | None = None,
    progress: BuildProgress | None = None,
) -> pystac.Catalog:
    """Build a static STAC catalog over the datacube (collection per source).

    Args:
        archive_root: Datacube archive root (local dir or ``s3://`` URI). Defaults
            to :attr:`ArchiveConfig.archive_root`.
        sources: Restrict to these source groups (default: all present).
        storage_options: fsspec options for a remote archive root.
        archive_config: Archive configuration (store name, etc.).
        stac_config: STAC configuration (catalog id/title, bbox policy, ...).
        progress: Optional :class:`BuildProgress` callbacks for progress reporting.

    Returns:
        The in-memory :class:`pystac.Catalog`. Persist it with :func:`write_catalog`.
    """
    archive_config = archive_config or ArchiveConfig()
    stac_config = stac_config or StacConfig()
    root = str(archive_root or archive_config.archive_root)
    so = storage_options if storage_options is not None else (archive_config.storage_options or None)

    reader = ArchiveReader(root, archive_config, storage_options=so)
    store_href = _store_href(root, archive_config.store)

    catalog = pystac.Catalog(
        id=stac_config.catalog_id,
        title=stac_config.catalog_title,
        description=stac_config.catalog_description,
    )

    src_list = list(sources) if sources is not None else reader.list_sources()
    prog = progress or BuildProgress()
    _emit(prog.on_sources, src_list)
    if not src_list:
        logger.warning(f"No source groups found under {store_href}.")

    for source_id in src_list:
        collection = build_source_collection(
            reader,
            source_id,
            store_href,
            config=stac_config,
            storage_options=so,
            progress=prog,
        )
        if collection is not None:
            catalog.add_child(collection)
    return catalog


# ── persistence ─────────────────────────────────────────────────────────────


def _write_catalog_remote(
    catalog: pystac.Catalog,
    dest: str,
    storage_options: dict[str, Any] | None,
    *,
    max_workers: int = 32,
) -> None:
    """Stage a self-contained catalog locally then upload the whole JSON tree to S3.

    Every file is uploaded recursively (catalog, collections and all items), in
    parallel. Deeper files go first so the ``catalog.json``/``collection.json``
    parents only appear once the items they link to exist.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    import boto3
    from botocore.config import Config

    no_scheme = dest[len("s3://") :]
    bucket, _, prefix = no_scheme.partition("/")
    prefix = prefix.strip("/")

    client_kwargs: dict[str, Any] = {"config": Config(max_pool_connections=max_workers)}
    if storage_options:
        endpoint = storage_options.get("endpoint_url") or (storage_options.get("client_kwargs") or {}).get(
            "endpoint_url"
        )
        if endpoint:
            client_kwargs["endpoint_url"] = endpoint
    s3 = boto3.client("s3", **client_kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        catalog.normalize_hrefs(tmp)
        catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)
        root = Path(tmp)
        by_depth: dict[int, list[Path]] = defaultdict(list)
        for json_path in root.rglob("*.json"):
            by_depth[len(json_path.relative_to(root).parts)].append(json_path)
        total = sum(len(v) for v in by_depth.values())
        logger.info(f"Uploading {total} STAC JSON files → s3://{bucket}/{prefix}")

        def _upload(json_path: Path) -> None:
            key = (f"{prefix}/" if prefix else "") + json_path.relative_to(root).as_posix()
            s3.upload_file(str(json_path), bucket, key, ExtraArgs={"ContentType": "application/json"})

        uploaded = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for depth in sorted(by_depth, reverse=True):
                for fut in as_completed([pool.submit(_upload, p) for p in by_depth[depth]]):
                    fut.result()
                    uploaded += 1
                    if uploaded % 1000 == 0:
                        logger.info(f"Uploaded {uploaded}/{total} STAC JSON files")
    logger.info(f"Catalog saved → s3://{bucket}/{prefix} ({uploaded} JSON files)")


def write_catalog(
    catalog: pystac.Catalog,
    dest: str | Path,
    *,
    storage_options: dict[str, Any] | None = None,
) -> str:
    """Persist a catalog as a self-contained JSON tree (local dir or ``s3://``).

    Args:
        catalog: The in-memory catalog from :func:`build_datacube_catalog`.
        dest: Destination — a local directory or an ``s3://bucket/prefix`` URI.
        storage_options: fsspec/boto3 options for remote destinations.

    Returns:
        The destination string.
    """
    dest = str(dest)
    if "://" in dest and not dest.startswith("file://"):
        _write_catalog_remote(catalog, dest, storage_options)
        return dest

    target = dest[len("file://") :] if dest.startswith("file://") else dest
    Path(target).mkdir(parents=True, exist_ok=True)
    catalog.normalize_hrefs(target)
    catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)
    logger.info(f"Catalog saved → {target}")
    return target
