#!/usr/bin/env python3
"""Download the KuroSiwo catalogue geopackage to assets/ks_catalogue.gpkg.

The catalogue (~500 MB) was previously tracked via Git LFS but has been
removed to avoid large downloads on clone.  This script fetches it from
the upstream Dropbox mirror on demand.

Usage:
    python scripts/download_kurosiwo.py
    # or via the CLI (if wired):
    uv run atlantis download-kurosiwo
"""

from __future__ import annotations

import hashlib
import os
import ssl
import sys
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OUTPUT_PATH = _REPO_ROOT / "assets" / "ks_catalogue.gpkg"

# Dropbox direct-download URL (dl=1 forces raw file download).
_DROPBOX_URL = (
    "https://www.dropbox.com/scl/fi/wu6nvj73cz4h7k3gxpzx6/catalogue.gpkg"
    "?rlkey=hsij2o0k60r2n0z6z4d2ngww9&st=0zjqhzgx&dl=1"
)

# Optional: expected SHA-256 for integrity verification.  Set to None to
# skip verification (useful when the upstream file is updated).
_EXPECTED_SHA256: str | None = None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _make_ssl_context(*, no_verify: bool = False) -> ssl.SSLContext:
    """Build an SSL context, respecting SSL_CERT_FILE / REQUESTS_CA_BUNDLE.

    Falls back to unverified context only when *no_verify* is True.
    """
    if no_verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # Honour common CA bundle env vars (corporate proxies often set these).
    ca_file = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if ca_file and Path(ca_file).exists():
        ctx = ssl.create_default_context(cafile=ca_file)
        return ctx

    # Try certifi if installed.
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
        return ctx
    except ImportError:
        pass

    # Try common system CA bundle paths.
    for ca_path in (
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
    ):
        if Path(ca_path).exists():
            ctx = ssl.create_default_context(cafile=ca_path)
            return ctx

    return ssl.create_default_context()


def _download(url: str, dest: Path, *, no_verify: bool = False) -> None:
    """Stream-download *url* to *dest* with a progress indicator."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".gpkg.part")

    print(f"Downloading KuroSiwo catalogue → {dest.relative_to(_REPO_ROOT)}")
    print(f"  Source: {url.split('?')[0]}...")

    try:
        ssl_ctx = _make_ssl_context(no_verify=no_verify)
        req = urllib.request.Request(url, headers={"User-Agent": "atlantis/1.0"})
        with urllib.request.urlopen(req, timeout=120, context=ssl_ctx) as resp:  # noqa: S310
            total = resp.headers.get("Content-Length")
            total_mb = f" / {int(total) / 1e6:.0f} MB" if total else ""
            downloaded = 0
            with open(tmp, "wb") as out:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    print(f"\r  {downloaded / 1e6:.1f} MB{total_mb}", end="", flush=True)
        print()  # newline after progress
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        print(f"\nDownload failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Integrity check (if hash is configured).
    if _EXPECTED_SHA256 is not None:
        actual = _sha256(tmp)
        if actual != _EXPECTED_SHA256:
            tmp.unlink(missing_ok=True)
            print(
                f"Hash mismatch!\n  expected: {_EXPECTED_SHA256}\n  actual:   {actual}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"  SHA-256 verified: {actual[:16]}...")

    tmp.rename(dest)
    print(f"  Saved: {dest.relative_to(_REPO_ROOT)} ({dest.stat().st_size / 1e6:.1f} MB)")


def main() -> None:
    no_verify = "--no-verify" in sys.argv
    if no_verify:
        print("  ⚠ SSL verification disabled (--no-verify)")

    if _OUTPUT_PATH.exists() and _OUTPUT_PATH.stat().st_size > 0:
        print(f"Already exists: {_OUTPUT_PATH.relative_to(_REPO_ROOT)} — skipping download.")
        print("  Delete the file and re-run to force a fresh download.")
        return

    _download(_DROPBOX_URL, _OUTPUT_PATH, no_verify=no_verify)


if __name__ == "__main__":
    main()
