"""NiceGUI application instance, page router, and layout shell."""

from __future__ import annotations

from nicegui import ui

from atlantis.ui.pages.fetch import fetch_page


def create_app():
    """Create and configure the NiceGUI application.

    Called once per process in ``launch()``. Sets up the page router,
    layout shell, and shared styles.
    """
    # Load .env so EARTHDATA_TOKEN etc. are available to fetchers.
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent.parent.parent / ".env", override=False)

    # Enable Atlantis logging so users see fetch progress in the terminal.
    from loguru import logger

    logger.enable("atlantis")

    # Pre-load available sources for form dropdowns
    try:
        from atlantis.fetchers.registry import list_fetchers
        from atlantis.layers.registry import load_source_registries

        load_source_registries()
        list_fetchers()  # triggers import of fetcher modules
    except Exception:
        pass

    @ui.page("/")
    def _index():
        _render_shell()
        fetch_page()

    # The @ui.page decorators register routes on the global ui module.
    # The caller then starts the server with ui.run(host=..., port=...).


def _render_shell() -> None:
    """Render shared layout: header bar."""
    with ui.header(elevated=True).classes("bg-cyan-700 text-white").style("position: sticky; top: 0; z-index: 1000"):
        with ui.row().classes("items-center w-full px-4 py-2"):
            ui.icon("water", size="md")
            ui.label("Atlantis Flood Dashboard").classes("text-xl font-bold")
            ui.space()
            ui.label("ML-ready satellite flood observations").classes("text-sm opacity-80")
