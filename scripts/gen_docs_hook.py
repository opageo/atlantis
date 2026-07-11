"""MkDocs pre-build hook: regenerate layer docs, copy external Markdown files.

Wired in ``mkdocs.yml`` via ``hooks:``. Also runnable standalone for testing::

    python scripts/gen_docs_hook.py
"""

from __future__ import annotations

import re
from pathlib import Path

from atlantis.layers.docs import render_all_markdown

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"

_COPIES: dict[Path, Path] = {
    REPO_ROOT / "README.md": DOCS_DIR / "index.md",
    REPO_ROOT / "src" / "README.md": DOCS_DIR / "architecture.md",
    REPO_ROOT / "CLI_Examples.md": DOCS_DIR / "cli-examples.md",
}

_KNOWN_REWRITES: dict[str, str] = {
    "src/README.md": "architecture.md",
    "CLI_Examples.md": "cli-examples.md",
    "docs/README.md": "sources.md",
    "docs/kurosiwo-stac-design.md": "kurosiwo/stac-design.md",
    "docs/mermaid_stac_ks_labeled.md": "kurosiwo/stac_ks_labeled.md",
    "docs/mermaid_stac_ks.md": "kurosiwo/stac_ks.md",
}


def on_pre_build(**kwargs: object) -> None:
    """MkDocs pre-build entry point — regenerates layer docs and copies external files."""
    _regenerate_all()


def _regenerate_all() -> None:
    # 1. Regenerate layer docs from registry
    layers_md = DOCS_DIR / "layers.md"
    layers_md.write_text(render_all_markdown(), encoding="utf-8")

    # 2. Copy external Markdown files into docs/
    for src, dst in _COPIES.items():
        text = src.read_text(encoding="utf-8")
        text = _fix_relative_links(text, src)
        dst.write_text(text, encoding="utf-8")

    # 3. Rename docs/README.md -> docs/sources.md
    sources_src = DOCS_DIR / "README.md"
    sources_dst = DOCS_DIR / "sources.md"
    if sources_src.exists():
        text = sources_src.read_text(encoding="utf-8")
        text = _fix_relative_links(text, sources_src)
        sources_dst.write_text(text, encoding="utf-8")


def _fix_relative_links(text: str, src_path: Path) -> str:
    """Rewrite internal doc links so they resolve correctly from ``docs/``."""
    src_dir = src_path.parent

    def _replace_url(match: re.Match[str]) -> str:
        prefix = match.group(1)  # ]( or src="
        url = match.group(2)
        suffix = match.group(3)  # ) or "

        # Skip external URLs and anchor-only links
        if url.startswith(("http://", "https://", "#")):
            return match.group(0)

        # Split path and anchor fragment
        if "#" in url:
            path_part, anchor = url.split("#", 1)
        else:
            path_part, anchor = url, None

        # Resolve the link relative to the source file's directory
        resolved = str((src_dir / path_part).resolve().relative_to(REPO_ROOT))

        # Map known external-to-docs paths
        if resolved in _KNOWN_REWRITES:
            new_path = _KNOWN_REWRITES[resolved]
        elif resolved.startswith("docs/"):
            new_path = resolved[5:]  # strip docs/ prefix
        else:
            return match.group(0)  # unchanged (e.g. notebooks/README.md)

        if anchor:
            new_path += "#" + anchor

        return f"{prefix}{new_path}{suffix}"

    # Markdown inline links: [text](path.md) or [text](path.md#anchor)
    text = re.sub(r"(\]\()([^)]+)(\))", _replace_url, text)
    # HTML src attributes: src="docs/..."
    text = re.sub(r'(src=")([^"]+)(")', _replace_url, text)
    return text


if __name__ == "__main__":
    _regenerate_all()
    print("Pre-build hook complete.")
