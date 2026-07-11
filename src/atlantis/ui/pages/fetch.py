"""Fetch page: form-driven pipeline (bbox/date/source -> search -> fetch -> harmonise -> plot)."""

from __future__ import annotations

from pathlib import Path

from nicegui import app, ui

from atlantis.ui.components.forms import (
    EVENT_PRESETS,
    bbox_input,
    date_range_picker,
    gfm_options,
    modis_options,
    option_toggle,
    source_selector,
    strategy_selector,
    viirs_options,
)
from atlantis.ui.components.map_viewer import flood_map_plotly
from atlantis.ui.components.status import (
    ActivityLogWidget,
    diagnostic_card,
    error_banner,
    fetch_progress_bar,
)
from atlantis.ui.models import FetchProgress, FetchRequest, FetchResponse
from atlantis.ui.services.fetch_service import run_fetch


def _build_request() -> FetchRequest:
    """Read form values from app.storage.user and build a FetchRequest."""
    s = app.storage.user
    date_range = s.get("date_range", {})
    return FetchRequest(
        event_id=s.get("event_id", ""),
        bbox=f"{s.get('bbox_west', 0)} {s.get('bbox_south', 0)} {s.get('bbox_east', 0)} {s.get('bbox_north', 0)}",
        start_date=date_range.get("from", "") if isinstance(date_range, dict) else "",
        end_date=date_range.get("to", "") if isinstance(date_range, dict) else "",
        source=s.get("source", "viirs"),
        classify=s.get("classify", True),
        stream=s.get("stream", True),
        harmonise=s.get("harmonise", False),
        plot=s.get("plot", False),
        strategy=s.get("strategy", "peak"),
        viirs_backend=s.get("viirs_backend", "noaa_s3"),
        modis_backend=s.get("modis_backend", "lance_geotiff"),
        modis_composite=s.get("modis_composite", "F2"),
        gfm_coarsen_factor=s.get("gfm_coarsen_factor", 4),
        gfm_resampling=s.get("gfm_resampling", "average"),
    )


def _render_results(response: FetchResponse) -> None:
    """Render fetch results: map, download links, diagnostics."""
    if response.error:
        error_banner(response.error)
        return

    with ui.column().classes("gap-4 w-full mt-4"):
        ui.label(f"Results for {response.event_id} ({response.source_id})").classes("text-xl font-bold")

        if response.files:
            ui.label(f"{len(response.files)} file(s) written").classes("text-base text-gray-600")

        # Show plot in a clickable card — opens a large dialog on click.
        if response.plot_path and response.plot_path.exists():
            _render_plot_card(response)

        elif response.harmonised_path and response.harmonised_path.exists():
            fig = flood_map_plotly(
                geotiff_path=response.harmonised_path,
                is_classified=True,
                title=response.event_id,
            )
            if fig is not None:
                ui.plotly(fig).classes("w-full h-96")

        # Download links
        if response.files:
            with ui.expansion("Download Files", icon="download").classes("w-full mt-4"):
                for f in sorted(response.files):
                    if f.exists():
                        ui.button(
                            f"{f.name} ({_size_str(f)})",
                            on_click=lambda f=f: ui.download(str(f)),
                            icon="download",
                        ).props("flat dense")

        if response.harmonised_path and response.harmonised_path.exists():
            with ui.expansion("Harmonised File", icon="layers").classes("w-full"):
                ui.button(
                    str(response.harmonised_path.name),
                    on_click=lambda p=response.harmonised_path: ui.download(str(p)),
                    icon="download",
                ).props("flat dense")

        if response.plot_path and response.plot_path.exists():
            with ui.expansion("Plot PNG", icon="image").classes("w-full"):
                ui.button(
                    str(response.plot_path.name),
                    on_click=lambda p=response.plot_path: ui.download(str(p)),
                    icon="download",
                ).props("flat dense")

        if not response.files and response.diagnostics is not None:
            diagnostic_card(response.diagnostics)


def _render_plot_card(response: FetchResponse) -> None:
    """Render a plot card with a button to open the large dialog."""
    with ui.card().classes("w-full p-3"):
        with ui.row().classes("w-full items-center justify-between mb-2"):
            ui.label("Peak-flood map").classes("text-base font-semibold")
            ui.button(
                "Enlarge",
                icon="open_in_full",
                on_click=lambda: _open_plot_dialog(response),
            ).props("flat dense size=sm color=cyan")

        try:
            fig = flood_map_plotly(
                geotiff_path=response.plot_path,
                is_classified=True,
                title="",
            )
            if fig is not None:
                fig.update_layout(margin={"l": 10, "r": 10, "t": 10, "b": 10}, height=280)
                ui.plotly(fig).classes("w-full")
            else:
                ui.image(str(response.plot_path)).classes("w-full rounded-lg max-h-64 object-contain")
        except Exception:
            ui.image(str(response.plot_path)).classes("w-full rounded-lg max-h-64 object-contain")


def _open_plot_dialog(response: FetchResponse) -> None:
    """Open a large dialog showing the full-resolution plot."""
    with ui.dialog() as dialog, ui.card().classes("w-[95vw] max-w-5xl"):
        with ui.row().classes("w-full items-center justify-between mb-2"):
            ui.label(f"{response.event_id} — {response.source_id}").classes("text-lg font-bold")
            ui.button(icon="close", on_click=dialog.close).props("flat round dense")

        if response.plot_path and response.plot_path.exists():
            try:
                fig = flood_map_plotly(
                    geotiff_path=response.plot_path,
                    is_classified=True,
                    title="",
                )
                if fig is not None:
                    fig.update_layout(
                        margin={"l": 10, "r": 10, "t": 10, "b": 10},
                        height=800,
                    )
                    ui.plotly(fig).classes("w-full")
                else:
                    ui.image(str(response.plot_path)).classes("w-full rounded-lg")
            except Exception:
                ui.image(str(response.plot_path)).classes("w-full rounded-lg")

    dialog.open()


def _size_str(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _apply_preset(preset: dict, storage: dict) -> None:
    """Fill form state from an event preset."""
    west, south, east, north = preset["bbox"]
    storage.update(
        {
            "event_id": preset["event_id"],
            "bbox_west": west,
            "bbox_south": south,
            "bbox_east": east,
            "bbox_north": north,
            "date_range": {"from": preset["date_from"], "to": preset["date_to"]},
        }
    )


def _init_storage() -> None:
    """Seed session storage with default values on first visit."""
    defaults: dict[str, object] = {
        "event_id": "Valencia_2024",
        "source": "viirs",
        "bbox_west": -1.5,
        "bbox_south": 38.8,
        "bbox_east": 0.5,
        "bbox_north": 40.0,
        "date_range": {"from": "2024-10-29", "to": "2024-11-04"},
        "classify": True,
        "stream": True,
        "harmonise": True,
        "plot": True,
        "strategy": "peak",
        "viirs_backend": "noaa_s3",
        "modis_backend": "lance_geotiff",
        "modis_composite": "F2",
        "gfm_coarsen_factor": 4,
        "gfm_resampling": "average",
    }
    for key, val in defaults.items():
        if key not in app.storage.user:
            app.storage.user[key] = val


def fetch_page(drawer=None) -> None:
    """Render the Fetch page with form in left drawer and results in main area.

    Args:
        drawer: A ``ui.left_drawer`` element whose context hosts the form.
            The main content (progress, results, log) renders outside it.
    """
    _init_storage()

    # Mutable containers so the async submit callback can reach UI elements
    # that are created after the form button.
    state: dict[str, object] = {"progress": None, "results": None, "log": None, "fetch_btn": None}

    async def _on_submit() -> None:
        request = _build_request()

        if not request.event_id or not request.start_date or not request.end_date:
            ui.notify("Please fill in Event ID, Start Date, and End Date.", type="warning")
            return

        btn = state["fetch_btn"]
        try:
            if btn:
                btn.enabled = False
                btn.text = "Fetching..."
        except Exception:
            pass

        try:
            progress_area = state["progress"]
            results_area = state["results"]
            log_widget = state["log"]

            progress_area.clear()
            results_area.clear()

            log_widget.log(f"Starting fetch for {request.event_id} ({request.source})")
            ui.notify(
                f"Fetching {request.event_id} via {request.source}...",
                position="top",
                type="info",
                spinner=True,
            )

            with progress_area:
                progress_bar_area = ui.column()

            def update_progress(p: FetchProgress) -> None:
                try:
                    progress_bar_area.clear()
                    with progress_bar_area:
                        fetch_progress_bar(p)
                    if p.message:
                        log_widget.log(p.message, level="info" if p.stage != "error" else "error")
                except Exception:
                    pass

            update_progress(FetchProgress(stage="searching", message="Initialising..."))

            try:
                response = await run_fetch(request, update_progress)
            except Exception as exc:
                import traceback

                traceback.print_exc()
                log_widget.log(f"Fetch failed: {exc}", level="error")
                with results_area:
                    error_banner(f"Fetch failed: {exc}")
                update_progress(FetchProgress(stage="error", error=str(exc)))
                ui.notify(f"Fetch failed: {exc}", type="negative")
                return

            log_widget.log(f"Complete: {len(response.files)} file(s) written", level="success")
            ui.notify("Fetch complete!", type="positive")

            with results_area:
                _render_results(response)
        finally:
            try:
                if btn:
                    btn.enabled = True
                    btn.text = "Start Fetch"
            except Exception:
                pass

    # --- Drawer: fetch form ---
    if drawer is not None:
        with drawer:
            state["fetch_btn"] = _render_form(on_submit=_on_submit)

    # --- Main content area ---
    with ui.column().classes("gap-4 p-4 w-full"):
        ui.label("Flood Data Fetcher").classes("text-3xl font-bold mb-2")
        state["progress"] = ui.column()
        state["results"] = ui.column()
        log_widget = ActivityLogWidget(title="Fetch Log")
        log_widget.create()
        state["log"] = log_widget


def _render_form(on_submit) -> None:
    """Render the fetch form in the left panel."""
    s = app.storage.user

    with ui.column().classes("gap-4 p-3"):
        # ── Event pre-sets ─────────────────────────────────────────────────
        with ui.card().classes("w-full p-3"):
            ui.label("Quick Pre-sets").classes("text-sm font-semibold mb-2")
            with ui.row().classes("flex-wrap gap-1"):
                for preset in EVENT_PRESETS:
                    is_active = preset["event_id"] == s.get("event_id")
                    btn = ui.button(
                        preset["name"],
                        icon="push_pin",
                        on_click=lambda p=preset: _apply_preset(p, s),
                    ).props("flat dense size=sm")
                    if is_active:
                        btn.props("color=cyan")

        # ── Event Configuration ────────────────────────────────────────────
        with ui.card().classes("w-full p-3"):
            ui.label("Event Configuration").classes("text-sm font-semibold mb-2")
            event_input = (
                ui.input(
                    label="Event ID",
                    value=str(s["event_id"]),
                    placeholder="e.g. Valencia_2024",
                )
                .props("outlined dense")
                .classes("w-full")
            )
            event_input.on(
                "update:model-value",
                lambda e: s.update({"event_id": e.args[0] if e.args else ""}),
            )

            ui.label("Bounding Box").classes("text-xs text-gray-500 mt-3 mb-1")
            w, st, e, n = bbox_input()
            w.bind_value(s, "bbox_west")
            st.bind_value(s, "bbox_south")
            e.bind_value(s, "bbox_east")
            n.bind_value(s, "bbox_north")

            ui.label("Date Range").classes("text-xs text-gray-500 mt-3 mb-1")
            dr = s.get("date_range", {})
            date_picker, date_display = date_range_picker(
                default_from=dr.get("from") if isinstance(dr, dict) else None,
                default_to=dr.get("to") if isinstance(dr, dict) else None,
            )
            date_picker.bind_value(s, "date_range")

            def _update_date_display() -> None:
                dr_val = s.get("date_range", {})
                if isinstance(dr_val, dict) and dr_val.get("from") and dr_val.get("to"):
                    date_display.text = f"{dr_val['from']}  –  {dr_val['to']}"

            ui.timer(0.5, _update_date_display)

        # ── Source & Options ───────────────────────────────────────────────
        with ui.card().classes("w-full p-3"):
            ui.label("Source & Options").classes("text-sm font-semibold mb-2")
            src_select = source_selector(on_change=lambda v: s.update({"source": v}))
            src_select.bind_value(s, "source")

            with ui.expansion("Advanced Options", icon="tune").classes("w-full mt-2"):
                with ui.column().classes("gap-1"):
                    classify_switch = option_toggle(
                        "Classify",
                        tooltip="Emit derived layers instead of raw pixel codes",
                        value=s["classify"],
                    )
                    classify_switch.bind_value(s, "classify")

                    stream_switch = option_toggle(
                        "Stream",
                        tooltip="Stream tiles via GDAL /vsicurl/ instead of downloading",
                        value=s["stream"],
                    )
                    stream_switch.bind_value(s, "stream")

                    harmonise_switch = option_toggle(
                        "Harmonise",
                        tooltip="Reproject to 1 arcmin EPSG:4326 + normalise to [0,1]",
                        value=s["harmonise"],
                    )
                    harmonise_switch.bind_value(s, "harmonise")

                    plot_switch = option_toggle(
                        "Plot",
                        tooltip="Generate a static PNG of the peak-flood date",
                        value=s["plot"],
                    )
                    plot_switch.bind_value(s, "plot")

                    strat_select = strategy_selector(value=s["strategy"])
                    strat_select.bind_value(s, "strategy")

                    ui.separator()
                    ui.label("Source-Specific").classes("text-xs text-gray-500")

                    with ui.column().bind_visibility_from(s, "source", backward=lambda v: v == "viirs"):
                        vii = viirs_options()
                        vii.bind_value(s, "viirs_backend")

                    with ui.column().bind_visibility_from(s, "source", backward=lambda v: v == "modis"):
                        mb, mc = modis_options()
                        mb.bind_value(s, "modis_backend")
                        mc.bind_value(s, "modis_composite")

                    with ui.column().bind_visibility_from(s, "source", backward=lambda v: v == "gfm"):
                        gco, gre = gfm_options()
                        gco.bind_value(s, "gfm_coarsen_factor")
                        gre.bind_value(s, "gfm_resampling")

        # ── Submit ─────────────────────────────────────────────────────────
        fetch_btn = (
            ui.button(
                "Start Fetch",
                icon="cloud_download",
                on_click=on_submit,
            )
            .classes("w-full")
            .props("color=cyan")
        )

    return fetch_btn
