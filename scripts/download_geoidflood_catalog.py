#!/usr/bin/env python3
"""Download the GEOID-Flood tile catalogues to assets/geoidflood_tile_catalog.parquet.

The merged catalogue (~730 KB, both dataset trees) is the single spatial
metadata source for GEOID-Flood event-AoIs: per-tile WKB geometry, UTM CRS,
delineation times, split and countries. It ships on the Hugging Face Hub and
is fetched here on demand — no raster download is required.

Usage:
    pixi run download-geoidflood-catalog
    # or: uv run python scripts/download_geoidflood_catalog.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from atlantis.utils.geoidflood import GEOIDFLOOD_DEFAULT_CATALOGUE, fetch_geoidflood_catalog  # noqa: E402


def main() -> None:
    output = _REPO_ROOT / GEOIDFLOOD_DEFAULT_CATALOGUE
    if output.exists() and output.stat().st_size > 0:
        print(f"Already exists: {output.relative_to(_REPO_ROOT)} — skipping download.")
        print("  Delete the file and re-run to force a fresh download.")
        return

    print(f"Downloading GEOID-Flood tile catalogues → {output.relative_to(_REPO_ROOT)}")
    fetch_geoidflood_catalog(output)
    print(f"  Saved: {output.relative_to(_REPO_ROOT)} ({output.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
