"""Service for reading the local fetch cache."""

from __future__ import annotations

from pathlib import Path

from atlantis.config import get_config
from atlantis.ui.models import EventSummary


def _get_cache_dir() -> Path:
    """Return the configured cache directory."""
    return get_config().fetcher.cache_dir


def list_events(cache_dir: Path | None = None) -> list[EventSummary]:
    """Walk the raw cache directory and return summaries of cached events.

    Args:
        cache_dir: Override the cache directory.  Uses the configured
            ``ATLANTIS_CACHE_DIR`` (or ``~/.cache/atlantis``) by default.

    Returns:
        List of EventSummary, newest first.
    """
    cache_dir = cache_dir or _get_cache_dir()
    raw_dir = cache_dir / "raw"
    if not raw_dir.exists():
        return []

    summaries: list[EventSummary] = []
    for event_path in sorted(raw_dir.iterdir(), reverse=True):
        if not event_path.is_dir():
            continue
        sources, all_files, dates = _scan_event_dir(event_path)
        if not sources:
            continue
        summaries.append(
            EventSummary(
                event_id=event_path.name,
                sources=sources,
                file_count=len(all_files),
                dates=dates,
                root=event_path,
            )
        )
    return summaries


def _scan_event_dir(event_path: Path) -> tuple[list[str], list[Path], list[str]]:
    """Scan an event directory for sources, files, and date tokens."""
    sources: list[str] = []
    all_files: list[Path] = []
    dates: set[str] = set()

    for item in sorted(event_path.iterdir()):
        if not item.is_dir():
            continue
        sources.append(item.name)
        for sub in item.rglob("*"):
            if sub.is_file():
                all_files.append(sub)
                token = _extract_date_token(sub.stem)
                if token:
                    dates.add(token)

    return sources, all_files, sorted(dates)


def _extract_date_token(stem: str) -> str | None:
    """Try to pull an 8-digit date token from a filename stem."""
    import re

    m = re.search(r"(\d{4})(\d{2})(\d{2})", stem)
    if m is None:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def list_files(event_root: Path, source: str | None = None) -> list[Path]:
    """Return all files under an event's source directory.

    Args:
        event_root: The root directory of a cached event.
        source: Optional source filter (e.g. ``"viirs"``, ``"modis"``, ``"gfm"``).

    Returns:
        Paths to all files under the event root, optionally filtered by source.
    """
    search_dir = event_root / source if source else event_root
    if not search_dir.exists():
        return []
    return sorted(p for p in search_dir.rglob("*") if p.is_file())


def find_harmonised(event_root: Path, source: str) -> Path | None:
    """Find a harmonised GeoTIFF for an event/source."""
    harm_dir = event_root / source / "harmonised"
    if not harm_dir.exists():
        return None
    for p in harm_dir.rglob("*.tif"):
        return p
    return None


def find_plot(event_root: Path, source: str) -> Path | None:
    """Find a plot PNG for an event/source."""
    plot_dir = event_root / source / "plots"
    if not plot_dir.exists():
        # Also check older layout where plots may be at event root
        plot_dir = event_root / "plots"
    if not plot_dir.exists():
        return None
    for p in plot_dir.rglob("*.png"):
        if source in p.name:
            return p
    for p in plot_dir.rglob("*.png"):
        return p
    return None
