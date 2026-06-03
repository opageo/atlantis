"""Bootstrap utilities for Atlantis data assets.

Provides a reusable ``run_setup`` function used by both the CLI
(``atlantis setup``) and the standalone ``scripts/setup.py`` entry point.

The function walks a registry of required assets and for each one:
  * skips if the file already exists
  * attempts an automatic ``git restore`` when the file is tracked but missing
  * falls back to a manual instruction when auto-restore fails
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


# ── Asset registry ─────────────────────────────────────────────────────────
# Each entry: (label, relative path from repo root, tracked_in_git?)

ASSETS: list[tuple[str, Path, bool]] = [
    (
        "VIIRS AOI tile grid",
        Path("src/atlantis/fetchers/viirs/data/viirs_aois.geojson"),
        True,
    ),
    (
        "KuroSiwo catalogue",
        Path("assets/ks_catalogue.gpkg"),
        True,
    ),
]


# ── Helpers ────────────────────────────────────────────────────────────────


def _is_lfs_pointer(path: Path) -> bool:
    """Return True if *path* looks like a Git-LFS pointer instead of real data."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
        return head.startswith(b"version https://git-lfs.github.com/spec/v1")
    except OSError:
        return False


def _git_restore(path: Path) -> bool:
    """Try to restore *path* from the current git tree.  Returns True on success."""
    if shutil.which("git") is None:
        return False
    try:
        result = subprocess.run(
            ["git", "restore", str(path)],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _check_geo_dependency() -> bool:
    """Return True if the ``geo`` extra dependencies are importable."""
    try:
        import geopandas  # noqa: F401
        import shapely  # noqa: F401

        return True
    except ImportError:
        return False


# ── Main entry point ──────────────────────────────────────────────────────


def run_setup(
    *,
    auto_fix: bool = True,
    output: object | None = None,
) -> bool:
    """Bootstrap required data assets.

    Parameters
    ----------
    auto_fix:
        When *True* (the default) attempt to restore missing tracked
        files automatically via ``git restore``.
    output:
        An object with a ``print()`` method (e.g. ``rich.console.Console``).
        Falls back to the built-in ``print`` when *None*.

    Returns:
        *True* when all assets are present, *False* when something is
        still missing after attempts.
    """
    _print = output.print if output is not None else print

    _print("[bold]Atlantis setup[/bold]\n")

    # ── Step 1: geo dependencies ────────────────────────────────────────────
    if _check_geo_dependency():
        _print("[green][ok][/green]  geo dependencies (geopandas, shapely)")
    else:
        _print("[yellow][warn][/yellow]  geo dependencies not installed")
        _print("       Run: uv sync --extra geo\n")

    # ── Step 2: required assets ─────────────────────────────────────────────
    any_missing = False
    for label, rel_path, tracked in ASSETS:
        abs_path = _REPO_ROOT / rel_path

        if abs_path.exists() and abs_path.stat().st_size > 0:
            # Check for LFS pointer files
            if _is_lfs_pointer(abs_path):
                _print(f"[yellow][LFS-POINTER][/yellow] {label} — {rel_path}")
                _print(f"       Run: git lfs pull -- {rel_path}")
                any_missing = True
            else:
                _print(f"[green][ok][/green]  {label} — {rel_path}")
            continue

        # File missing or empty
        _print(f"[red][MISSING][/red] {label} — {rel_path}")

        if not tracked:
            _print(f"       File must be provided manually: {rel_path}")
            any_missing = True
            continue

        if auto_fix:
            restored = _git_restore(abs_path)
            if restored and abs_path.exists() and abs_path.stat().st_size > 0:
                _print("       [green]Restored from git[/green]")
            else:
                _print("       git restore failed. Manual restore:")
                _print(f"       git checkout HEAD -- {rel_path}")
                any_missing = True
        else:
            _print(f"       Restore with: git checkout HEAD -- {rel_path}")
            any_missing = True

    # ── Summary ──────────────────────────────────────────────────────────────
    _print("")
    if any_missing:
        _print("[yellow]Some assets are missing.  See messages above.[/yellow]")
        return False

    _print("[green][bold]All data assets are present.[/bold][/green]")
    return True


def get_missing_assets() -> list[str]:
    """Return a list of labels for assets that are missing.

    Useful for quick pre-flight checks before other commands.
    """
    missing: list[str] = []
    for label, rel_path, _tracked in ASSETS:
        abs_path = _REPO_ROOT / rel_path
        if not abs_path.exists() or abs_path.stat().st_size == 0:
            missing.append(label)
        elif _is_lfs_pointer(abs_path):
            missing.append(f"{label} (LFS pointer, not pulled)")
    return missing
