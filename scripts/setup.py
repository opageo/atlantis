#!/usr/bin/env python3
r"""Atlantis setup — bootstrap data assets required by the fetchers.

Run this **once** after cloning the repository (and whenever a new data source
is added to ensure its prerequisites are present).

Currently handles:
* VIIRS — global AOI tile grid (``src/atlantis/fetchers/viirs/data/viirs_aois.geojson``)
* KuroSiwo — catalogue (``assets/ks_catalogue.gpkg``)

Each step is idempotent — re-running skips assets that already exist.

Usage::

    uv run python scripts/setup.py

For auto-restore (default), missing tracked files are restored from git.
Use ``--check-only`` to only verify without modifying anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from rich.console import Console  # noqa: E402

from atlantis.utils.setup import run_setup  # noqa: E402


def main() -> None:
    """Bootstrap required data assets."""
    auto_fix = "--check-only" not in sys.argv
    success = run_setup(auto_fix=auto_fix, output=Console())
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
