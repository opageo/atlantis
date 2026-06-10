"""Bootstrap utilities for Atlantis data assets.

Provides a reusable ``run_setup`` function used by both the CLI
(``atlantis setup``) and the standalone ``scripts/setup.py`` entry point.

The function walks a registry of required assets and for each one:
  * skips if the file already exists
  * attempts an automatic ``git restore`` when the file is tracked but missing
  * falls back to a manual instruction when auto-restore fails
  * verifies SHA-256 integrity against ``config/asset_hashes.json``

Only assets required by the core workflow are registered here.  Optional
LFS-tracked assets (e.g. the KuroSiwo catalogue) are validated on demand
by the commands that use them, not as a global prerequisite.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_ASSET_HASHES_PATH = _REPO_ROOT / "config" / "asset_hashes.json"


# ── Asset registry ─────────────────────────────────────────────────────────
# Each entry: (label, relative path from repo root, tracked_in_git?)
#
# Only assets required by the core workflow (e.g. ``atlantis demo``) are
# listed here.  Optional LFS assets (e.g. KuroSiwo catalogue) are not
# included — they are validated on demand by the commands that need them.

ASSETS: list[tuple[str, Path, bool]] = [
    (
        "VIIRS AOI tile grid",
        Path("src/atlantis/fetchers/viirs/data/viirs_aois.geojson"),
        True,
    ),
]

# Assets that should be verified against the expected hash (excludes LFS).
_HASHED_ASSETS: frozenset[str] = frozenset(
    {
        "src/atlantis/fetchers/viirs/data/viirs_aois.geojson",
    }
)


# ── Hash helpers ───────────────────────────────────────────────────────────


def _compute_sha256(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_asset_hashes() -> dict[str, str]:
    """Load expected hashes from ``config/asset_hashes.json``.

    Returns an empty dict when the file does not exist.
    """
    if not _ASSET_HASHES_PATH.exists():
        return {}
    with open(_ASSET_HASHES_PATH) as fh:
        return json.load(fh)


def _asset_expected_hash(rel_path: str) -> str | None:
    """Return the ``sha256:<hex>`` value for *rel_path*, or ``None``."""
    hashes = _load_asset_hashes()
    return hashes.get(rel_path)


def _write_asset_hashes(hashes: dict[str, str]) -> None:
    """Write (or overwrite) ``config/asset_hashes.json``."""
    _ASSET_HASHES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_ASSET_HASHES_PATH, "w") as fh:
        json.dump(hashes, fh, indent=2)
        fh.write("\n")


# ── File helpers ───────────────────────────────────────────────────────────


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
    update_hashes: bool = False,
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
    update_hashes:
        When *True*, recompute SHA-256 hashes for all non-LFS assets
        and write them to ``config/asset_hashes.json``.

    Returns:
        *True* when all assets are present, *False* when something is
        still missing after attempts.
    """
    _print = output.print if output is not None else print

    _print("[bold]Asset check[/bold]\n")

    # ── Step 1: geo dependencies ────────────────────────────────────────────
    if _check_geo_dependency():
        _print("[bold green]✓[/bold green]  geo dependencies (geopandas, shapely)")
    else:
        _print("[bold yellow]⚠[/bold yellow]  geo dependencies not installed")
        _print("       Run: uv sync --extra geo\n")

    # ── Step 2: required assets ─────────────────────────────────────────────
    any_missing = False
    hashes: dict[str, str] = {} if update_hashes else {}

    for label, rel_path, tracked in ASSETS:
        abs_path = _REPO_ROOT / rel_path
        rel_str = str(rel_path)

        if abs_path.exists() and abs_path.stat().st_size > 0:
            # Check for LFS pointer files
            if _is_lfs_pointer(abs_path):
                _print(f"[bold yellow]⚠[/bold yellow]  [LFS-POINTER] {label} — {rel_path}")
                _print("       Run: git lfs pull -- {rel_path}")
                any_missing = True
                continue

            # ── Hash verification (non-LFS assets only) ─────────────────
            if rel_str in _HASHED_ASSETS:
                expected = _asset_expected_hash(rel_str)
                if update_hashes:
                    actual_hash = _compute_sha256(abs_path)
                    hashes[rel_str] = f"sha256:{actual_hash}"
                    _print(f"[bold green]✓[/bold green]  {label} — {rel_path}")
                    _print(f"       sha256:{actual_hash}")
                elif expected is not None:
                    actual_hash = _compute_sha256(abs_path)
                    expected_hash = expected.removeprefix("sha256:")
                    if actual_hash != expected_hash:
                        _print(f"[bold yellow]⚠[/bold yellow]  [CHANGED] {label} — {rel_path}")
                        _print(f"       expected: {expected}")
                        _print(f"       actual:   sha256:{actual_hash}")
                        if auto_fix:
                            restored = _git_restore(abs_path)
                            if restored and abs_path.exists():
                                new_hash = _compute_sha256(abs_path)
                                if new_hash == expected_hash:
                                    _print("       [green]Restored from git — version matches[/green]")
                                else:
                                    _print(
                                        "       [yellow]Restored from git —"
                                        " but hash differs (may be intentional)[/yellow]"
                                    )
                            else:
                                _print("       git restore failed. Manual restore:")
                                _print(f"       git checkout HEAD -- {rel_path}")
                        any_missing = True
                    else:
                        _print(f"[bold green]✓[/bold green]  {label} — {rel_path}")
                else:
                    _print(f"[bold green]✓[/bold green]  {label} — {rel_path}")
            else:
                _print(f"[bold green]✓[/bold green]  {label} — {rel_path}")
            continue

        # File missing or empty
        _print(f"[bold red]✗[/bold red]  [MISSING] {label} — {rel_path}")

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
            _print(f"       Restore with: git checkout HEAD — {rel_path}")
            any_missing = True

    # ── Update hashes ───────────────────────────────────────────────────────
    if update_hashes and hashes:
        _write_asset_hashes(hashes)
        _print(f"\n[bold]Wrote updated hashes → {_ASSET_HASHES_PATH.relative_to(_REPO_ROOT)}[/bold]")

    # ── Summary ──────────────────────────────────────────────────────────────
    _print("")
    if any_missing:
        _print("[bold yellow]⚠[/bold yellow]  Some assets are missing or out of date.  See messages above.")
        return False

    _print("[bold green]✓[/bold green]  All data assets are present and up-to-date.")
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
        elif str(rel_path) in _HASHED_ASSETS:
            expected = _asset_expected_hash(str(rel_path))
            if expected is not None:
                actual_hash = _compute_sha256(abs_path)
                expected_hash = expected.removeprefix("sha256:")
                if actual_hash != expected_hash:
                    missing.append(f"{label} (hash mismatch)")
    return missing
