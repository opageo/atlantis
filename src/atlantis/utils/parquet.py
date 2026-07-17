"""Shared helpers for reading/writing Parquet (and GeoParquet) files.

These files may live locally or on a remote filesystem (``s3://``, ``gs://``,
``az://``, ...).
"""

from __future__ import annotations

from typing import Any


def pyarrow_filesystem_for(dest: str, storage_options: dict[str, Any] | None = None) -> tuple[str, Any]:
    """Resolve *dest* to a (path, pyarrow filesystem) pair for pyarrow writers.

    Local paths are returned unchanged with ``filesystem=None`` so pyarrow infers
    a local filesystem. Remote URIs (``s3://``, ``gs://``, ``az://``, ...) are
    resolved via fsspec — honouring *storage_options* (credentials, custom
    ``endpoint_url``, ...) — and wrapped as a ``pyarrow.fs.FileSystem``, since
    pyarrow's own URI resolution has no way to pick up those options.

    Args:
        dest: Destination path — local, or remote (``s3://``, ``gs://``, ...).
        storage_options: fsspec filesystem options for a remote *dest*.

    Returns:
        A ``(path, filesystem)`` pair suitable for ``pyarrow``-backed writers
        (e.g. ``geopandas.GeoDataFrame.to_parquet(path, filesystem=filesystem)``).
    """
    from urllib.parse import urlparse

    if urlparse(dest).scheme in ("", "file"):
        return dest, None

    import fsspec
    import pyarrow.fs

    fs, path = fsspec.core.url_to_fs(dest, **(storage_options or {}))
    return path, pyarrow.fs.PyFileSystem(pyarrow.fs.FSSpecHandler(fs))
