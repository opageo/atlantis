#!/usr/bin/env python3
r"""Atlantis setup — bootstrap data assets required by the fetchers.

Run this **once** after cloning the repository (and whenever a new data source
is added to ensure its prerequisites are present).

Currently handles:
* VIIRS — global AOI tile grid (``src/atlantis/fetchers/viirs/data/viirs_aois.geojson``)

Usage::

    uv run python scripts/setup.py

Each step is idempotent — re-running skips assets that already exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


# ═════════════════════════════════════════════════════════════════════════════
# Registry of setup steps — add new data sources here
# ═════════════════════════════════════════════════════════════════════════════

STEPS: list[tuple[str, Path]] = [
    (
        "VIIRS AOI grid",
        _REPO_ROOT / "src" / "atlantis" / "fetchers" / "viirs" / "data" / "viirs_aois.geojson",
    ),
]


def main() -> None:
    """Verify all required data assets are present (no-op when they are)."""
    print("Atlantis setup — verification\n")

    any_missing = False
    for label, path in STEPS:
        if path.exists():
            print(f"[ok]  {label} — {path.relative_to(_REPO_ROOT)}")
        else:
            any_missing = True
            print(f"[MISSING] {label} — {path.relative_to(_REPO_ROOT)}")
            print(f"         Restore with: git checkout -- {path.relative_to(_REPO_ROOT)}")

    if any_missing:
        print("\nSome assets are missing.  See instructions above.")
        sys.exit(1)
    else:
        print("\nAll data assets are present.")


if __name__ == "__main__":
    main()
