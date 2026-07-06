"""File browser component for cache directory tree and download links."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui


def file_tree_card(
    event_root: Path,
    source: str | None = None,
) -> None:
    """Render a collapsible file tree for a cached event.

    Each GeoTIFF and PNG is shown with a download link.

    Args:
        event_root: Root path of the cached event.
        source: Optional source filter (e.g. "viirs", "modis", "gfm").
    """
    from atlantis.ui.services.cache_service import list_files

    files = list_files(event_root, source)
    if not files:
        ui.label("No files found.").classes("text-gray-500 text-sm")
        return

    with ui.column().classes("gap-1 w-full max-h-80 overflow-y-auto"):
        for f in sorted(files):
            with ui.row().classes("items-center gap-2"):
                rel = f.relative_to(event_root)
                icon = "image" if f.suffix == ".png" else "map"
                ui.icon(icon, size="sm").classes("text-gray-500")
                ui.label(str(rel)).classes("text-sm text-gray-700 truncate flex-1")
                ui.label(f"{_human_size(f)}").classes("text-xs text-gray-400 ml-2")
                # Download link: serve via NiceGUI's downloads
                with ui.row().classes("gap-1"):
                    ui.button(
                        icon="download",
                        on_click=lambda f=f: ui.download(str(f)),
                    ).props("flat dense size=sm")


def _human_size(path: Path) -> str:
    """Return a human-readable file size."""
    try:
        size = path.stat().st_size
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
