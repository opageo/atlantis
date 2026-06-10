#!/usr/bin/env python3
r"""Atlantis setup — bootstrap data assets and credentials for the fetchers.

Run this **once** after cloning the repository (and whenever a new data source
is added to ensure its prerequisites are present).

Currently handles:
* VIIRS — global AOI tile grid (``src/atlantis/fetchers/viirs/data/viirs_aois.geojson``)
* KuroSiwo — catalogue (``assets/ks_catalogue.gpkg``)
* MODIS — NASA Earthdata bearer token (``EARTHDATA_TOKEN``)
* GFM — AWS profiles ``default`` (ECMWF object store, credentials required)
  and ``noa`` (anonymous AWS for public NOAA buckets) written to
  ``~/.aws/{config,credentials}``.

Each step is idempotent — re-running skips assets that already exist and
credentials / profiles that are already configured. Existing AWS profiles
are never overwritten; missing sections are added in-place.

Usage::

    uv run python scripts/setup.py                # interactive when stdin is a TTY
    uv run python scripts/setup.py --check-only   # verify only, no prompts/writes
    uv run python scripts/setup.py --non-interactive
    uv run python scripts/setup.py --update-hashes
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from rich.console import Console  # noqa: E402

from atlantis.utils.setup import run_setup  # noqa: E402


def main() -> None:
    """Bootstrap required data assets and credentials."""
    args = set(sys.argv[1:])
    check_only = "--check-only" in args
    update_hashes = "--update-hashes" in args
    non_interactive = "--non-interactive" in args or check_only

    auto_fix = not check_only
    interactive = None if not non_interactive else False

    console = Console()
    console.print("[bold cyan]Atlantis setup[/bold cyan] — checking assets, credentials, and AWS profiles\n")
    if interactive is None and sys.stdin.isatty():
        console.print(
            "[dim]Tip: you may be prompted to paste an Earthdata token "
            "(https://urs.earthdata.nasa.gov/) and ECMWF S3 access/secret "
            "keys. Have them ready before continuing.[/dim]\n"
        )

    success = run_setup(
        auto_fix=auto_fix,
        output=console,
        update_hashes=update_hashes,
        interactive=interactive,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
