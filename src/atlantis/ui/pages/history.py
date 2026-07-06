"""History page: browse cached events, re-view maps, re-download files."""

from __future__ import annotations

from nicegui import ui

from atlantis.ui.components.file_browser import file_tree_card
from atlantis.ui.components.map_viewer import flood_map_plotly
from atlantis.ui.services.cache_service import find_harmonised, find_plot, list_events


def history_page() -> None:
    """Render the History page with a scrollable card grid of cached events."""
    ui.label("Cached Events").classes("text-2xl font-bold mb-4")

    events = list_events()

    if not events:
        with ui.card().classes("w-full p-8 text-center"):
            ui.icon("inbox", size="lg").classes("text-gray-300 text-6xl")
            ui.label("No cached events yet.").classes("text-lg text-gray-500 mt-2")
            ui.label("Go to the Fetch page to get started.").classes("text-sm text-gray-400")
            ui.button("Go to Fetch", on_click=lambda: ui.navigate.to("/")).classes("mt-4")
        return

    ui.label(f"{len(events)} event(s) found").classes("text-sm text-gray-500 mb-4")

    with ui.scroll_area().classes("w-full h-[calc(100vh-180px)]"):
        with ui.column().classes("gap-4 p-2"):
            for summary in events:
                _render_event_card(summary)


def _render_event_card(summary) -> None:
    """Render a single event card with expandable file tree and map preview."""
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center justify-between w-full"):
            with ui.column().classes("gap-1"):
                ui.label(summary.event_id).classes("text-lg font-bold")
                ui.label(
                    f"{', '.join(summary.sources)} · {summary.file_count} files"
                ).classes("text-sm text-gray-500")
                if summary.dates:
                    ui.label(
                        f"Dates: {summary.dates[0]} → {summary.dates[-1]}"
                    ).classes("text-xs text-gray-400")

        with ui.expansion("View Files", icon="folder").classes("w-full mt-2"):
            file_tree_card(summary.root)

        with ui.expansion("Map Preview", icon="map").classes("w-full"):
            preview_source = summary.sources[0] if summary.sources else None
            if preview_source:
                harm = find_harmonised(summary.root, preview_source)
                plot_png = find_plot(summary.root, preview_source)
                if harm:
                    fig = flood_map_plotly(geotiff_path=harm, is_classified=True, title=summary.event_id)
                    if fig is not None:
                        ui.plotly(fig).classes("w-full h-80")
                elif plot_png:
                    ui.image(str(plot_png)).classes("max-w-full")
                else:
                    ui.label("No harmonised or plot file found for preview.").classes("text-sm text-gray-400")
            else:
                ui.label("No sources found.").classes("text-sm text-gray-400")
