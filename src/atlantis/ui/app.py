"""NiceGUI application instance, page router, and layout shell."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

from atlantis.ui.pages.fetch import fetch_page

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def create_app():
    """Create and configure the NiceGUI application.

    Called once per process in ``launch()``. Sets up the page router,
    layout shell, and shared styles.
    """
    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env", override=False)

    from loguru import logger

    logger.enable("atlantis")

    try:
        from atlantis.fetchers.registry import list_fetchers
        from atlantis.layers.registry import load_source_registries

        load_source_registries()
        list_fetchers()
    except Exception:
        pass

    @ui.page("/", title="Atlantis")
    def _index():
        _render_fetch_page()


def _render_fetch_page() -> None:
    """Render the page shell with header, left drawer, and main content.

    The header's waves icon toggles the drawer open/closed. The drawer
    holds the fetch form; the main area shows results, progress, and log.
    """
    drawer_ref = {}

    with ui.header(elevated=True).classes("bg-cyan-700 text-white").style("position: sticky; top: 0; z-index: 1000"):
        with ui.row().classes("items-center w-full px-4 py-2"):
            ui.button(icon="water", on_click=lambda: drawer_ref.get("toggle", lambda: None)()).props("flat round")
            ui.label("Atlantis Flood Dashboard").classes("text-xl font-bold")
            ui.space()
            ui.label("ML-ready satellite flood observations").classes("text-sm opacity-80")

    with ui.left_drawer(value=True, elevated=True).classes("bg-gray-50").props("width=360") as drawer:
        drawer_ref["toggle"] = drawer.toggle

    fetch_page(drawer)
