"""CLI entrypoints for Atlantis."""

import typer

cli = typer.Typer(help="Atlantis — ML-ready flood inundation archive pipeline.")


@cli.command()
def fetch():
    """Fetch raw inundation data."""
    typer.echo("fetch: not yet implemented")


@cli.command()
def archive():
    """Harmonise and write ML-ready archive."""
    typer.echo("archive: not yet implemented")


@cli.command()
def validate():
    """Validate the archive."""
    typer.echo("validate: not yet implemented")
