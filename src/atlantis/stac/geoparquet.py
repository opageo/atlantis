"""Export the datacube STAC items to a stac-geoparquet index.

This is the serverless **scale path** for discovery: a single columnar Parquet
file of all items (next to the Zarr store on S3) that can be queried with
geopandas/DuckDB — fast bbox/datetime search over thousands of items without
standing up a STAC API server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

from loguru import logger

if TYPE_CHECKING:
    import geopandas as gpd
    import pystac

__all__ = ["export_items_to_geoparquet", "search_geoparquet"]


def _as_dicts(items: "Iterable[pystac.Item | dict[str, Any]]") -> list[dict[str, Any]]:
    """Coerce STAC items (pystac or dict) to a list of GeoJSON-like dicts."""
    return [it.to_dict() if hasattr(it, "to_dict") else it for it in items]


def _pyarrow_filesystem_for(dest: str, storage_options: dict[str, Any] | None) -> tuple[str, Any]:
    """Resolve *dest* to a (path, pyarrow filesystem) pair for ``pq.ParquetWriter``.

    Local paths are returned unchanged with ``filesystem=None`` so pyarrow infers
    a local filesystem. Remote URIs (``s3://``, ``gs://``, ``az://``, ...) are
    resolved via fsspec — honouring *storage_options* (credentials, custom
    ``endpoint_url``, ...) — and wrapped as a ``pyarrow.fs.FileSystem``, since
    pyarrow's own URI resolution has no way to pick up those options.
    """
    from urllib.parse import urlparse

    if urlparse(dest).scheme in ("", "file"):
        return dest, None

    import fsspec
    import pyarrow.fs

    fs, path = fsspec.core.url_to_fs(dest, **(storage_options or {}))
    return path, pyarrow.fs.PyFileSystem(pyarrow.fs.FSSpecHandler(fs))


def export_items_to_geoparquet(
    items: "Iterable[pystac.Item | dict[str, Any]]",
    dest: str,
    *,
    storage_options: dict[str, Any] | None = None,
) -> str:
    """Write STAC *items* to a stac-geoparquet file at *dest*.

    Tries the modern ``stac_geoparquet.arrow`` API first, then falls back to the
    legacy ``to_geodataframe`` API for older versions.

    Args:
        items: STAC items (pystac ``Item`` objects or item dicts).
        dest: Output ``.parquet`` path — local, or remote (``s3://``, ``gs://``, ...).
        storage_options: fsspec filesystem options for a remote *dest* (credentials,
            ``anon``, custom ``endpoint_url`` via ``client_kwargs``, ...).

    Returns:
        The destination path.
    """
    item_dicts = _as_dicts(items)
    if not item_dicts:
        raise ValueError("No items to export.")

    # Modern arrow-based API (stac-geoparquet >= 0.5).
    try:
        from stac_geoparquet import arrow as sg_arrow

        batches = sg_arrow.parse_stac_items_to_arrow(item_dicts)
        write_path, filesystem = _pyarrow_filesystem_for(dest, storage_options)
        sg_arrow.to_parquet(batches, write_path, filesystem=filesystem)
        logger.info(f"Wrote {len(item_dicts)} item(s) → {dest} (arrow API)")
        return dest
    except Exception as exc:  # noqa: BLE001 - fall back across API versions
        logger.debug(f"stac_geoparquet.arrow unavailable ({exc}); falling back to to_geodataframe.")

    import stac_geoparquet

    gdf = stac_geoparquet.to_geodataframe(item_dicts)
    write_path, filesystem = _pyarrow_filesystem_for(dest, storage_options)
    gdf.to_parquet(write_path, filesystem=filesystem)
    logger.info(f"Wrote {len(item_dicts)} item(s) → {dest} (geodataframe API)")
    return dest


def search_geoparquet(
    path: str,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    start: str | None = None,
    end: str | None = None,
) -> "gpd.GeoDataFrame":
    """Filter a stac-geoparquet index by bbox and/or inclusive date range.

    Args:
        path: Path to the ``.parquet`` index (local or remote, via geopandas).
        bbox: Optional ``(west, south, east, north)`` spatial filter (intersects).
        start: Optional inclusive start date (ISO ``YYYY-MM-DD``).
        end: Optional inclusive end date (ISO ``YYYY-MM-DD``).

    Returns:
        The matching rows as a GeoDataFrame.
    """
    import geopandas as gpd
    import pandas as pd

    gdf = gpd.read_parquet(path)

    if bbox is not None:
        from shapely.geometry import box

        gdf = gdf[gdf.intersects(box(*bbox))]
    if (start is not None or end is not None) and "datetime" in gdf.columns:
        dt = pd.to_datetime(gdf["datetime"], utc=True)
        if start is not None:
            gdf = gdf[dt >= pd.Timestamp(start, tz="UTC")]
        if end is not None:
            # inclusive end-of-day
            gdf = gdf[dt <= pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)]
    return gdf
