"""Store resolution for local filesystem and remote (S3) Zarr archives.

The archive root may be a local path (``/data/atlantis``) or a cloud URI
(``s3://atlantis/cube``). This helper returns a Zarr-v3-compatible store target
for a named sub-store under the root, usable by both ``zarr.open_group`` and
``xarray.open_zarr``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import zarr.storage


def is_remote(root: str | Path) -> bool:
    """Return True if *root* is a remote URI (e.g. ``s3://``), not a local path."""
    s = str(root)
    return "://" in s and not s.startswith("file://")


def store_for(
    root: str | Path,
    name: str,
    storage_options: dict[str, Any] | None = None,
) -> "Path | zarr.storage.FsspecStore":
    """Resolve the store target for sub-store *name* under *root*.

    Args:
        root: Archive root — a local directory or a remote URI (``s3://...``).
        name: Sub-store name (e.g. ``"raw.zarr"``).
        storage_options: fsspec options forwarded for remote stores
            (credentials, endpoint, ``anon``, ...).

    Returns:
        A local :class:`~pathlib.Path` for filesystem roots, or a
        :class:`zarr.storage.FsspecStore` for remote roots.
    """
    if is_remote(root):
        import zarr.storage

        url = str(root).rstrip("/") + "/" + name
        return zarr.storage.FsspecStore.from_url(url, storage_options=storage_options or None)
    return Path(root) / name
