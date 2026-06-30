"""Generate the per-source layer documentation from the registries.

The layer registries (``atlantis.fetchers.<source>.layers``) are the single
source of truth. Run this script to regenerate ``docs/layers.md`` whenever a
native or derived layer changes so the docs never drift from the code::

    python scripts/generate_layer_docs.py

Pass an explicit output path to write elsewhere::

    python scripts/generate_layer_docs.py path/to/layers.md
"""

from __future__ import annotations

import sys
from pathlib import Path

from atlantis.layers.docs import render_all_markdown

DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "layers.md"


def main() -> None:
    """Render the registries to Markdown and write the docs file."""
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_all_markdown(), encoding="utf-8")
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
