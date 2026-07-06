"""Status display components: progress bars, diagnostic cards, error banners, activity log."""

from __future__ import annotations

from nicegui import ui

from atlantis.ui.models import FetchProgress


class ActivityLogWidget:
    """Scrollable activity log following the vresto ActivityLogWidget pattern.

    Messages are appended as ``ui.label`` entries inside a ``ui.scroll_area``.
    The ``column`` attribute can be used directly to add messages from any scope.
    """

    def __init__(self, title: str = "Activity Log") -> None:
        """Initialise the log widget.

        Args:
            title: Heading shown above the scrollable log area.
        """
        self.title = title
        self.column = None

    def create(self) -> None:
        """Build the log UI element inline."""
        with ui.card().classes("w-full p-3 shadow-sm rounded-lg"):
            ui.label(self.title).classes("text-sm font-semibold mb-2")
            with ui.scroll_area().classes("w-full h-48"):
                self.column = ui.column().classes("w-full gap-1")

    def log(self, text: str, *, level: str = "info") -> None:
        """Append a message to the log.

        Args:
            text: Message text.
            level: One of ``"info"``, ``"success"``, ``"warning"``, ``"error"``.
        """
        if self.column is None:
            return
        color = {
            "info": "text-gray-700",
            "success": "text-green-700",
            "warning": "text-amber-600",
            "error": "text-red-600",
        }.get(level, "text-gray-700")
        with self.column:
            ui.label(text).classes(f"text-xs {color} break-words")


STAGE_ORDER = ["idle", "searching", "fetching", "harmonising", "plotting", "done"]
STAGE_LABELS = {
    "idle": "Waiting",
    "searching": "Search",
    "fetching": "Fetch",
    "harmonising": "Harmonise",
    "plotting": "Plot",
    "done": "Complete",
    "error": "Error",
}


def fetch_progress_bar(progress: FetchProgress) -> None:
    """Render step indicators as a horizontal stepper.

    Shown during an active fetch to indicate which pipeline stage is current.
    """
    if progress.stage == "error":
        ui.icon("error", color="red").classes("text-2xl")
        ui.label(f"Error: {progress.error or 'Unknown error'}").classes("text-red-500")
        return

    current_idx = STAGE_ORDER.index(progress.stage) if progress.stage in STAGE_ORDER else 0
    with ui.row().classes("gap-1 items-center"):
        for i, stage in enumerate(STAGE_ORDER):
            if stage == "idle":
                continue
            label = STAGE_LABELS[stage]
            if i < current_idx:
                ui.icon("check_circle", color="green").classes("text-sm")
                ui.label(label).classes("text-green-700 text-xs")
            elif i == current_idx and progress.stage != "done":
                ui.spinner(size="sm").classes("text-sm")
                ui.label(label).classes("text-blue-600 text-xs font-bold")
            else:
                ui.icon("radio_button_unchecked", color="gray").classes("text-sm")
                ui.label(label).classes("text-gray-400 text-xs")

        if progress.stage == "done":
            ui.icon("check_circle", color="green").classes("text-lg ml-4")
            if progress.message:
                ui.label(progress.message).classes("text-green-700 text-sm")


def diagnostic_card(diagnostics) -> None:
    """Render structured diagnostics in a styled card.

    Translates source-specific diagnostics fields into actionable guidance.
    """
    if diagnostics is None:
        return

    with ui.card().classes("bg-amber-50 border border-amber-300 mt-4 p-4 w-full"):
        ui.label("No results found").classes("text-lg font-bold text-amber-800")
        with ui.column().classes("gap-2 mt-2"):

            miss = getattr(diagnostics, "missing_aoi_coverage", False)
            if miss:
                ui.label(
                    "Event bbox does not intersect any packaged AOI. Widen the bbox or verify coordinates."
                ).classes("text-sm text-amber-700")

            year_gap = getattr(diagnostics, "year_coverage_gap", False)
            if year_gap:
                published = getattr(diagnostics, "available_years", [])
                requested = getattr(diagnostics, "requested_years", [])
                pub_str = ", ".join(str(y) for y in sorted(published)) if published else "unknown"
                req_str = ", ".join(str(y) for y in sorted(requested))
                ui.label(f"Backend doesn't publish data for year(s) {req_str}. Published: {pub_str}").classes(
                    "text-sm text-amber-700"
                )

            net = getattr(diagnostics, "network_unreachable", False)
            if net:
                ui.label(
                    f"Backend '{getattr(diagnostics, 'backend', '?')}' is unreachable. Check network."
                ).classes("text-sm text-amber-700")
                last_err = getattr(diagnostics, "last_network_error", None)
                if last_err:
                    ui.label(f"Last error: {last_err}").classes("text-xs text-amber-600")

            empty = getattr(diagnostics, "listings_all_empty", False)
            if empty:
                ui.label(
                    f"Backend returned no listings for the {getattr(diagnostics, 'dates_probed', '?')} requested dates."
                ).classes("text-sm text-amber-700")

            no_aoi = getattr(diagnostics, "no_aoi_match_in_listings", False)
            if no_aoi:
                ui.label("Dates had listings but none contained tiles for AOI.").classes(
                    "text-sm text-amber-700"
                )

            no_items = getattr(diagnostics, "no_items_found", False)
            if no_items:
                ui.label(
                    "STAC search returned no items for this bbox and date range. Try widening the dates."
                ).classes("text-sm text-amber-700")

            tm = getattr(diagnostics, "auth_token_missing", False)
            if tm:
                ui.label(
                    "EARTHDATA_TOKEN not set. Register at https://urs.earthdata.nasa.gov/"
                ).classes("text-sm text-amber-700")

            outside_lance = getattr(diagnostics, "outside_lance_window", False)
            if outside_lance:
                ui.label(
                    "Dates fall outside LANCE NRT window. Try modis_backend=laads_hdf4 for historical dates."
                ).classes("text-sm text-amber-700")

            tile_count = getattr(diagnostics, "tile_count", None)
            if tile_count is not None and not tile_count:
                ui.label("BBox maps to zero MODIS tiles (likely dateline-crossing).").classes(
                    "text-sm text-amber-700"
                )

            no_tile_match = getattr(diagnostics, "no_tile_match_in_listings", False)
            if no_tile_match:
                ui.label("Listings had no tiles matching the intersecting tiles. Widen the date window.").classes(
                    "text-sm text-amber-700"
                )


def error_banner(message: str, on_retry: callable | None = None) -> None:
    """Red banner with error message and optional retry button.

    Args:
        message: Error text to display.
        on_retry: If provided, a "Retry" button is shown that calls this callback.
    """
    from nicegui import ui

    with ui.card().classes("bg-red-50 border border-red-300 mt-4 p-4 w-full"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("error", color="red").classes("text-xl")
            ui.label(message).classes("text-red-700 text-sm")
            if on_retry is not None:
                ui.button("Retry", on_click=on_retry, color="red").props("flat")
