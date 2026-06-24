"""I/O utility functions for downloading and caching."""

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from loguru import logger

if TYPE_CHECKING:
    pass

# Default cache directory
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "atlantis"

# Domains that should retain the Authorization header across redirects.
# LAADS/LANCE servers redirect through urs.earthdata.nasa.gov for OAuth;
# Python requests strips auth headers on cross-domain redirects by default.
_NASA_AUTH_HOSTS = frozenset(
    {
        "urs.earthdata.nasa.gov",
        "ladsweb.modaps.eosdis.nasa.gov",
        "nrt3.modaps.eosdis.nasa.gov",
        "nrt4.modaps.eosdis.nasa.gov",
        "e4ftl01.cr.usgs.gov",
    }
)


class _EarthdataSession(requests.Session):
    """A requests Session that preserves Authorization across NASA redirects.

    By default ``requests`` strips the ``Authorization`` header when the
    response redirects to a different hostname.  NASA's Earthdata Login flow
    redirects between ``ladsweb.modaps.eosdis.nasa.gov`` and
    ``urs.earthdata.nasa.gov``; dropping the Bearer token causes the server
    to return an HTML login page instead of the binary data file.

    This override keeps the header when *both* the origin and target belong
    to NASA's Earthdata ecosystem (see ``_NASA_AUTH_HOSTS``), and raises
    immediately if the server redirects to the OAuth authorize endpoint
    (which means the token was rejected).
    """

    def rebuild_auth(self, prepared_request, response):  # noqa: D102
        headers = prepared_request.headers
        url = prepared_request.url

        if "Authorization" in headers:
            redirect_parsed = requests.utils.urlparse(url)
            redir_host = redirect_parsed.hostname or ""
            redir_path = redirect_parsed.path or ""

            # If we're being redirected to the OAuth authorize endpoint, the
            # server rejected our token.  Abort early with a clear message
            # instead of sending the LAADS token to URS (which returns 401).
            if redir_host == "urs.earthdata.nasa.gov" and "/oauth/authorize" in redir_path:
                raise DownloadContentError(
                    "LAADS server rejected the Bearer token and redirected to Earthdata OAuth login. "
                    "Verify your EARTHDATA_TOKEN is a valid LAADS application token from "
                    "https://ladsweb.modaps.eosdis.nasa.gov/profiles/#app-tokens "
                    "(not an Earthdata Login access token)."
                )

            original_parsed = requests.utils.urlparse(response.request.url)
            orig_host = original_parsed.hostname or ""

            # Keep the header if both hosts are in the NASA trust set.
            if orig_host in _NASA_AUTH_HOSTS and redir_host in _NASA_AUTH_HOSTS:
                return
            # Also keep if both share the same parent domain (*.eosdis.nasa.gov).
            if orig_host.endswith(".eosdis.nasa.gov") and redir_host.endswith(".eosdis.nasa.gov"):
                return

        super().rebuild_auth(prepared_request, response)


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


class HtmlResponseError(RuntimeError):
    """Raised when an HTTP download yields an HTML page instead of binary data.

    Typically indicates an authentication/EULA-acceptance redirect (e.g. NASA
    Earthdata / LAADS DAAC sending users to ``/profiles/licenses/...`` or
    ``/oauth/login`` when the user has not approved the data archive yet).
    """


def _looks_like_html(payload: bytes) -> bool:
    """Return True when *payload* starts with an HTML document marker."""
    head = payload.lstrip()[:256].lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def download_file(
    url: str,
    output_path: Path | None = None,
    cache_dir: Path | None = None,
    headers: dict | None = None,
    chunk_size: int = 8192,
    progress: bool = True,
    headers: dict[str, str] | None = None,
) -> Path:
    """Download a file from URL with caching support.

    Refuses to cache HTML responses (typical of auth/EULA redirects) so that
    a bad first request does not poison the cache.

    Args:
        url: URL to download.
        output_path: Output file path. If None, uses cache.
        cache_dir: Cache directory. Defaults to ~/.cache/atlantis.
        headers: Optional HTTP headers to include in the request.
        chunk_size: Download chunk size in bytes.
        progress: Whether to show progress bar.
        headers: Optional HTTP headers (e.g. bearer token).

    Returns:
        Path to downloaded file.

    Raises:
        ImportError: If requests is not installed.
        HtmlResponseError: If the server returned HTML (likely an auth/EULA
            redirect). The partial file is removed before raising.
    """
    destination = output_path or get_cache_path(url, cache_dir)
    ensure_dir(destination.parent)

    if destination.exists():
        logger.debug("Already cached: {}", destination)
        return destination

    logger.debug("Downloading {} -> {}", url, destination)
    response = requests.get(url, stream=True, timeout=60, headers=headers or {})
    response.raise_for_status()

    # Reject HTML payloads up front via the Content-Type header. NASA LAADS,
    # for example, returns 200 + an HTML login page when the user has not
    # accepted the data archive EULA.
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" in content_type or "application/xhtml" in content_type:
        raise HtmlResponseError(
            f"Server returned HTML (Content-Type: {content_type or 'unknown'}) for {url}. "
            "This usually means an authentication/EULA redirect. For NASA Earthdata + LAADS "
            "DAAC: log in at https://urs.earthdata.nasa.gov/, then visit the file URL once "
            "in a browser and accept the LAADS DAAC license prompt."
        )

    with destination.open("wb") as file_handle:
        first_chunk = True
        for chunk in response.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            # Sniff the first chunk: some servers return text/plain or omit
            # Content-Type entirely while still serving an HTML login page.
            if first_chunk and _looks_like_html(chunk):
                file_handle.close()
                destination.unlink(missing_ok=True)
                raise HtmlResponseError(
                    f"Server returned HTML body for {url}. "
                    "This usually means an authentication/EULA redirect. For NASA "
                    "Earthdata + LAADS DAAC: log in at https://urs.earthdata.nasa.gov/, "
                    "then visit the file URL once in a browser and accept the LAADS "
                    "DAAC license prompt."
                )
            first_chunk = False
            file_handle.write(chunk)

    etag = response.headers.get("ETag")
    if etag:
        set_etag(destination, etag)

    return destination


class DownloadContentError(RuntimeError):
    """Raised when a download returns unexpected content (e.g. HTML login page)."""


def _validate_not_html(first_bytes: bytes, url: str) -> None:
    """Raise if the first bytes of a download look like an HTML page."""
    # Check the leading bytes (case-insensitive) for common HTML markers.
    header = first_bytes[:64].lstrip()
    if header[:1] == b"<" and (header.lower().startswith(b"<!doctype html") or header.lower().startswith(b"<html")):
        raise DownloadContentError(
            f"Expected binary data but the response body is HTML. "
            f"URL: {url} — this usually indicates an authentication failure or redirect."
        )


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
