"""Static event-bookmark registry — named shortcuts for bbox/date-range/sources.

A *bookmark* is a curated, user-managed entry (``event_id`` -> bbox + inclusive
date range + optional default sources) stored as a small **GeoParquet** file so
``atlantis fetch --event NAME`` can resolve ``--bbox``/``--start-date``/
``--end-date`` without repeating them on every invocation. Entries are managed
via ``atlantis bookmarks add/remove`` (or the functions below).

This is **not** the same thing as the ``atlantis_events`` registry recorded per
source inside the Zarr archive by ``ArchiveWriter.write(..., event=...)`` (see
``atlantis.archive.writer``/``atlantis.archive.reader``): that registry is
data-driven — it only knows about events for which data has actually been
archived. This module's registry is static and independent of the archive; it
exists purely to save typing common bbox/date combinations at fetch time.

The registry is a GeoDataFrame with one row per bookmark:

* ``event_id`` — unique bookmark name (str).
* ``geometry`` — the bbox as a ``shapely`` box polygon (CRS ``EPSG:4326``).
* ``start_date`` / ``end_date`` — inclusive date range, ISO ``YYYY-MM-DD`` strings.
* ``sources`` — comma-joined default source list (may be empty).
* ``label`` — optional human-readable description.
* ``updated_at`` — ISO timestamp of the last write.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import fsspec
import geopandas as gpd
import pandas as pd
from loguru import logger
from shapely.geometry import box

from atlantis.archive._store import is_remote
from atlantis.config import get_config
from atlantis.models.event import FloodEvent
from atlantis.utils.parquet import pyarrow_filesystem_for

if TYPE_CHECKING:
    import geopandas as gpd

    from atlantis.config import BookmarksConfig

__all__ = [
    "bookmark_path",
    "load_bookmarks",
    "save_bookmarks",
    "list_bookmarks",
    "get_bookmark",
    "add_bookmark",
    "remove_bookmark",
]

_COLUMNS = ("event_id", "start_date", "end_date", "sources", "label", "updated_at")


def bookmark_path(config: "BookmarksConfig | None" = None) -> str:
    """Resolve the bookmarks GeoParquet path from *config* (or the global config).

    Args:
        config: Bookmarks configuration. Defaults to ``get_config().bookmarks``.

    Returns:
        A local path or ``s3://...`` URI to the bookmarks file.
    """

    config = config or get_config().bookmarks
    root = config.bookmarks_root
    if is_remote(root):
        return root.rstrip("/") + "/" + config.bookmarks_file
    return str(Path(root).expanduser() / config.bookmarks_file)


def _exists(path: str, storage_options: dict[str, Any] | None) -> bool:
    """Return True if a file already exists at *path* (local or remote)."""

    if urlparse(path).scheme in ("", "file"):
        return Path(path).exists()

    fs, fpath = fsspec.core.url_to_fs(path, **(storage_options or {}))
    return fs.exists(fpath)


def _empty_frame() -> "gpd.GeoDataFrame":
    """Build an empty, correctly-typed bookmarks GeoDataFrame."""

    return gpd.GeoDataFrame(
        {col: pd.Series([], dtype="object") for col in _COLUMNS},
        geometry=gpd.GeoSeries([], dtype="geometry"),
        crs="EPSG:4326",
    )


def _resolved_storage_options(storage_options: dict[str, Any] | None) -> dict[str, Any] | None:

    if storage_options is not None:
        return storage_options
    return get_config().bookmarks.storage_options or None


def load_bookmarks(
    path: str | None = None,
    storage_options: dict[str, Any] | None = None,
) -> "gpd.GeoDataFrame":
    """Load the bookmarks registry, or an empty one if it doesn't exist yet.

    Args:
        path: Bookmarks file path (local or ``s3://``). Defaults to :func:`bookmark_path`.
        storage_options: fsspec options for a remote *path*.

    Returns:
        A GeoDataFrame with columns ``event_id, geometry, start_date, end_date,
        sources, label, updated_at``.
    """
    import geopandas as gpd

    path = path or bookmark_path()
    storage_options = _resolved_storage_options(storage_options)
    if not _exists(path, storage_options):
        return _empty_frame()
    return gpd.read_parquet(path)


def save_bookmarks(
    gdf: "gpd.GeoDataFrame",
    path: str | None = None,
    storage_options: dict[str, Any] | None = None,
) -> str:
    """Write the bookmarks registry to *path* (local or remote).

    Args:
        gdf: The bookmarks GeoDataFrame to persist.
        path: Destination path. Defaults to :func:`bookmark_path`.
        storage_options: fsspec options for a remote *path*.

    Returns:
        The destination path.
    """

    path = path or bookmark_path()
    storage_options = _resolved_storage_options(storage_options)

    _write_path, filesystem = pyarrow_filesystem_for(path, storage_options)
    write_path = Path(_write_path)
    if filesystem is None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(
        write_path,
        filesystem=filesystem,
        schema_version="1.1.0",
        write_covering_bbox=True,
        index=False,
    )
    return path


def list_bookmarks(
    path: str | None = None,
    storage_options: dict[str, Any] | None = None,
) -> list[str]:
    """Return all registered bookmark event ids, sorted alphabetically."""
    gdf = load_bookmarks(path=path, storage_options=storage_options)
    return sorted(gdf["event_id"].tolist())


def _as_date(value: Any) -> Any:

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _row_to_event(row: Any) -> FloodEvent:
    bbox = tuple(round(float(v), 6) for v in row.geometry.bounds)
    sources_raw = row.get("sources") or ""
    sources = [s for s in str(sources_raw).split(",") if s]
    return FloodEvent(
        event_id=row["event_id"],
        bbox=bbox,  # type: ignore[arg-type]
        start_date=_as_date(row["start_date"]),
        end_date=_as_date(row["end_date"]),
        sources=sources,
    )


def get_bookmark(
    event_id: str,
    path: str | None = None,
    storage_options: dict[str, Any] | None = None,
) -> FloodEvent:
    """Resolve a bookmark to a :class:`~atlantis.models.event.FloodEvent`.

    Args:
        event_id: The bookmark name.
        path: Bookmarks file path. Defaults to :func:`bookmark_path`.
        storage_options: fsspec options for a remote *path*.

    Returns:
        A ``FloodEvent`` built from the bookmarked bbox/date-range/sources.

    Raises:
        KeyError: If no bookmark named *event_id* is registered.
    """
    gdf = load_bookmarks(path=path, storage_options=storage_options)
    matches = gdf[gdf["event_id"] == event_id]
    if matches.empty:
        raise KeyError(f"Bookmark '{event_id}' not found.")
    return _row_to_event(matches.iloc[0])


def add_bookmark(
    event: FloodEvent,
    label: str | None = None,
    path: str | None = None,
    storage_options: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> str:
    """Register (or replace) a bookmark.

    Args:
        event: The event to bookmark — ``event_id``, ``bbox``, ``start_date``,
            ``end_date`` and (optionally) default ``sources`` are stored.
        label: Optional human-readable description.
        path: Bookmarks file path. Defaults to :func:`bookmark_path`.
        storage_options: fsspec options for a remote *path*.
        overwrite: If False (default) and ``event.event_id`` already exists,
            raises ``ValueError``.

    Returns:
        The destination path.

    Raises:
        ValueError: If the bookmark already exists and ``overwrite`` is False.
    """

    gdf = load_bookmarks(path=path, storage_options=storage_options)
    exists = bool((gdf["event_id"] == event.event_id).any())
    if exists and not overwrite:
        raise ValueError(f"Bookmark '{event.event_id}' already exists (use --force / overwrite=True to replace it).")

    new_row = gpd.GeoDataFrame(
        {
            "event_id": [event.event_id],
            "start_date": [event.start_date],
            "end_date": [event.end_date],
            "sources": [event.sources],
            "label": [label or ""],
            "updated_at": [datetime.now(tz=timezone.utc)],
        },
        geometry=[box(*event.bbox)],
        crs="EPSG:4326",
    )
    gdf = gdf[gdf["event_id"] != event.event_id]
    gdf = pd.concat([gdf, new_row], ignore_index=True)
    dest = save_bookmarks(gdf, path=path, storage_options=storage_options)
    logger.info(f"Bookmark '{event.event_id}' saved → {dest}")
    return dest


def remove_bookmark(
    event_id: str,
    path: str | None = None,
    storage_options: dict[str, Any] | None = None,
) -> str:
    """Remove a bookmark by id.

    Args:
        event_id: The bookmark name to remove.
        path: Bookmarks file path. Defaults to :func:`bookmark_path`.
        storage_options: fsspec options for a remote *path*.

    Returns:
        The destination path.

    Raises:
        KeyError: If no bookmark named *event_id* is registered.
    """
    gdf = load_bookmarks(path=path, storage_options=storage_options)
    if not bool((gdf["event_id"] == event_id).any()):
        raise KeyError(f"Bookmark '{event_id}' not found.")
    gdf = gdf[gdf["event_id"] != event_id].reset_index(drop=True)
    dest = save_bookmarks(gdf, path=path, storage_options=storage_options)
    logger.info(f"Bookmark '{event_id}' removed → {dest}")
    return dest
