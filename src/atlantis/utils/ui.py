"""Shared Rich UI helpers for the Atlantis CLI.

All commands should import ``console``, ``command_header``, and the glyph
helpers from here so the visual style stays consistent across the CLI.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.rule import Rule
from rich.table import Table, box
from rich.text import Text
from rich.tree import Tree

# ── Shared console instance ───────────────────────────────────────────────────
# A single Console is used across the entire CLI so that Progress live regions
# and plain console.print() calls coordinate properly.  Rich auto-detects
# whether the output is a TTY and disables animations / ANSI codes when it
# isn't (e.g. CI pipes).

console = Console()

# ── Branding ──────────────────────────────────────────────────────────────────

APP_NAME = "Atlantis"


def command_header(title: str, subtitle: str | None = None) -> None:
    """Print a compact styled panel as the command header.

    Example output (TTY):
    ╭─ Atlantis · fetch ─────────────────────────────────────────╮
    │  Valencia_2024 · sources=viirs                             │
    ╰────────────────────────────────────────────────────────────╯
    """
    heading = Text.assemble(
        (f"{APP_NAME}", "bold cyan"),
        (" · ", "dim"),
        (title, "bold white"),
    )
    content: Text | str = heading
    if subtitle:
        content = Text.assemble(heading, "\n", Text(subtitle, style="dim"))
    console.print(
        Panel(content, border_style="cyan", padding=(0, 1)),
        highlight=False,
    )


# ── Section separators ────────────────────────────────────────────────────────


def section_rule(label: str) -> None:
    """Print a subtle horizontal rule with a label."""
    console.print(Rule(f" {label} ", style="cyan dim"))


# ── Status glyphs ─────────────────────────────────────────────────────────────
# These replace ad-hoc [green]…[/green] / [yellow]…[/yellow] usage so that
# every status line is immediately scannable.


def ok(msg: str) -> None:
    """Print a success line: ``✓  <msg>``."""
    console.print(f"[bold green]✓[/bold green]  {msg}")


def warn(msg: str) -> None:
    """Print a warning line: ``⚠  <msg>``."""
    console.print(f"[bold yellow]⚠[/bold yellow]  {msg}")


def fail(msg: str) -> None:
    """Print a failure line: ``✗  <msg>``."""
    console.print(f"[bold red]✗[/bold red]  {msg}")


def info(msg: str) -> None:
    """Print an informational line: ``ℹ  <msg>``."""
    console.print(f"[bold blue]ℹ[/bold blue]  {msg}")


def skip(msg: str) -> None:
    """Print a skipped line: ``·  <msg>``."""
    console.print(f"[dim]·[/dim]  {msg}")


# ── Progress bar factory ──────────────────────────────────────────────────────


def make_progress() -> Progress:
    """Return a pre-configured ``rich.Progress`` instance.

    Columns: spinner · description · bar · M/N · elapsed · remaining.
    The Progress is *not* started here; use it as a context manager::

        with make_progress() as progress:
            task = progress.add_task("Cases", total=n)
            for item in items:
                …
                progress.advance(task)
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )


# ── Spinner context manager ───────────────────────────────────────────────────


@contextmanager
def step_status(message: str) -> Iterator[None]:
    """Context manager that shows a spinner with *message* while the block runs.

    On non-TTY terminals the spinner is suppressed automatically by Rich.
    Successful completion is silent; the caller should print a result line
    after the ``with`` block.
    """
    with console.status(message, spinner="dots"):
        yield


# ── File-list tree ────────────────────────────────────────────────────────────


def file_tree(root_label: str, paths: list) -> Tree:
    """Build a Rich ``Tree`` listing file paths under a root label.

    ``paths`` may be :class:`pathlib.Path` objects or strings.  Only the
    final component (basename) is shown as the leaf label.
    """
    tree = Tree(f"[bold]{root_label}[/bold]")
    for path in paths:
        tree.add(f"[dim]{path}[/dim]")
    return tree


# ── Summary table ─────────────────────────────────────────────────────────────


def summary_table(title: str, columns: list[str], rows: list[list[str]]) -> Table:
    """Build and return a styled summary ``Table``.

    Args:
        title:   Table title (shown above the header row).
        columns: Ordered column header names.
        rows:    List of rows; each row is a list of strings aligned to *columns*.
    """
    table = Table(
        title=title,
        box=box.SIMPLE_HEAVY,
        title_style="bold",
        header_style="bold cyan",
        show_lines=False,
        expand=False,
    )
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*row)
    return table
