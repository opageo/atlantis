"""I/O utility functions for downloading and caching."""

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    pass

# Default cache directory
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "atlantis"


def ensure_dir(path: Path) -> Path:
    """Ensure a directory exists, creating it if necessary.

    Args:
        path: Directory path.

    Returns:
        The directory path.
    """
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_cache_path(url: str, cache_dir: Path | None = None) -> Path:
    """Get the cache file path for a URL.

    Args:
        url: URL to get cache path for.
        cache_dir: Cache directory. Defaults to ~/.cache/atlantis.

    Returns:
        Path to cache file.
    """
    cache_dir = cache_dir or DEFAULT_CACHE_DIR
    # Create a hash of the URL for the filename (non-cryptographic use)
    url_hash = hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()
    # Use extension from URL if available
    extension = Path(url).suffix or ""
    return cache_dir / f"{url_hash}{extension}"


def download_file(
    url: str,
    output_path: Path | None = None,
    cache_dir: Path | None = None,
    chunk_size: int = 8192,
    progress: bool = True,
) -> Path:
    """Download a file from URL with caching support.

    Args:
        url: URL to download.
        output_path: Output file path. If None, uses cache.
        cache_dir: Cache directory. Defaults to ~/.cache/atlantis.
        chunk_size: Download chunk size in bytes.
        progress: Whether to show progress bar.

    Returns:
        Path to downloaded file.

    Raises:
        ImportError: If requests is not installed.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - exercised by environment setup
        raise ImportError("requests is required to download files") from exc

    destination = output_path or get_cache_path(url, cache_dir)
    ensure_dir(destination.parent)

    if destination.exists():
        logger.debug("Already cached: {}", destination)
        return destination

    logger.debug("Downloading {} -> {}", url, destination)
    response = requests.get(url, stream=True, timeout=60)
    response.raise_for_status()

    with destination.open("wb") as file_handle:
        for chunk in response.iter_content(chunk_size=chunk_size):
            if chunk:
                file_handle.write(chunk)

    etag = response.headers.get("ETag")
    if etag:
        set_etag(destination, etag)

    return destination


def get_etag(path: Path) -> str | None:
    """Get the ETag of a cached file.

    Args:
        path: Path to cached file.

    Returns:
        ETag string or None if not cached.
    """
    etag_file = path.with_suffix(path.suffix + ".etag")
    if etag_file.exists():
        return etag_file.read_text().strip()
    return None


def set_etag(path: Path, etag: str) -> None:
    """Set the ETag for a cached file.

    Args:
        path: Path to cached file.
        etag: ETag string.
    """
    etag_file = path.with_suffix(path.suffix + ".etag")
    etag_file.write_text(etag)
