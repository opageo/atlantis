"""Typer sub-app for the `atlantis web` CLI commands."""

from __future__ import annotations

import typer

web_app = typer.Typer(help="Atlantis web dashboard.")


@web_app.command("launch")
def launch(
    host: str = "127.0.0.1",
    port: int = 8080,
    reload: bool = False,
) -> None:
    """Start the Atlantis web dashboard."""
    import secrets

    from atlantis.ui.app import create_app

    create_app()
    import nicegui

    nicegui.ui.run(
        host=host,
        port=port,
        reload=reload,
        storage_secret=secrets.token_hex(32),
    )
