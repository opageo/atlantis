"""Live-updating checklist renderable for the Atlantis CLI.

Renders a vertical list of steps as ``☐ name`` / animated spinner / ``✓ name`` /
``⚠ name`` / ``✗ name`` rows inside a Rich ``Live`` display. Rows are
updated in place as each step transitions through its lifecycle, giving
the user a single-glance view of what is running, what is done, and what
failed.

Why this exists
---------------
The CLI previously used a separate spinner + post-hoc ``ok()`` /
``warn()`` line per step. That model scatters progress across the
terminal and offers no at-a-glance view of what is still pending. This
``Checklist`` replaces it with a compact, persistent ledger of all
orchestrated steps for the current command.

Verbosity modes
---------------
* Default (``verbose=False``): CLI-orchestrated steps only (Fetch,
  Plot, Harmonise, …). No processor sub-step ticks are emitted.
* Verbose (``verbose=True``): a loguru handler is installed that watches
  known processor debug strings (``"Mosaicked …"``, ``"Clipped to AOI"``,
  …) and surfaces them as nested sub-steps under the currently running
  parent row. See :class:`LoguruChecklistHandler`.

No Textual
----------
Rich's :class:`rich.live.Live` is sufficient: fetch loops are
synchronous, ``Live`` coordinates with the shared ``console``, and
non-TTY terminals degrade to static text automatically. Textual would
require a full app rewrite.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator, Sequence

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from atlantis.utils.ui import console as shared_console

# ── Status enum & glyph mapping ──────────────────────────────────────────────


@dataclass(frozen=True)
class Status:
    """String constants representing the lifecycle state of a checklist row."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    WARN = "warn"
    FAIL = "fail"


# Glyphs per status. Using Unicode box-drawing characters + standard
# checks so the rows render consistently across TTYs.
_GLYPHS: dict[str, tuple[str, str]] = {
    Status.PENDING: ("☐", "dim"),
    Status.DONE: ("✓", "bold green"),
    Status.WARN: ("⚠", "bold yellow"),
    Status.FAIL: ("✗", "bold red"),
}


def _glyph(status: str) -> RenderableType:
    if status == Status.RUNNING:
        return Spinner("dots", style="cyan")
    char, style = _GLYPHS[status]
    return Text(char, style=style)


# ── Item + Checklist ─────────────────────────────────────────────────────────


@dataclass
class ChecklistItem:
    """One row in the checklist."""

    item_id: str
    name: str
    status: str = Status.PENDING
    detail: str = ""
    spinner: Spinner | None = None
    # Sub-steps are rendered as indented child rows underneath this item.
    # Only populated when verbose mode is active.
    substeps: list["ChecklistItem"] = field(default_factory=list)


@dataclass(frozen=True)
class ProfileBinding:
    """Runtime binding of a log-driven animation profile to checklist rows."""

    name: str
    process_item_id: str
    pre_item_id: str | None = None


_FETCH_RASTER_SUBSTEPS = ("Mosaic tiles", "Clip to AOI", "Classify pixels")

_SUBSTEP_PROFILES: dict[str, tuple[str, ...]] = {
    "viirs_fetch": _FETCH_RASTER_SUBSTEPS,
    "modis_fetch": _FETCH_RASTER_SUBSTEPS,
}


class Checklist:
    """A mutable, re-renderable list of :class:`ChecklistItem` rows.

    Designed to be used as the renderable inside a :class:`Live` block::

        with task_checklist(["Fetch tiles", "Plot", "Harmonise"]) as cl:
            with cl.step("Fetch tiles"):
                fetcher.fetch(...)
            with cl.step("Plot"):
                _plot_source(...)
    """

    def __init__(self, *, title: str | None = None) -> None:
        """Initialise an empty checklist with an optional heading."""
        self._items: dict[str, ChecklistItem] = {}
        self._order: list[str] = []
        self._counter: int = 0
        self._title = title
        self._log_lines: deque[str] = deque(maxlen=8)
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────

    def add(self, name: str) -> str:
        """Add a new pending row. Returns its id."""
        with self._lock:
            self._counter += 1
            item_id = f"item_{self._counter}"
            self._items[item_id] = ChecklistItem(item_id=item_id, name=name)
            self._order.append(item_id)
            return item_id

    def start(self, item_id: str) -> None:
        """Mark *item_id* as currently running."""
        self._set_status(item_id, Status.RUNNING)

    def complete(self, item_id: str, *, detail: str = "") -> None:
        """Mark *item_id* as successfully completed."""
        self._set_status(item_id, Status.DONE, detail=detail)

    def warn(self, item_id: str, *, detail: str = "") -> None:
        """Mark *item_id* as completed with a warning (non-fatal)."""
        self._set_status(item_id, Status.WARN, detail=detail)

    def fail(self, item_id: str, *, detail: str = "") -> None:
        """Mark *item_id* as failed."""
        self._set_status(item_id, Status.FAIL, detail=detail)

    def add_substep(self, parent_id: str, name: str) -> str:
        """Add a nested sub-step under *parent_id*. Returns the new id.

        Sub-steps are rendered as indented rows underneath their parent.
        Use this for verbose-mode processor sub-step reporting.
        """
        with self._lock:
            parent = self._items.get(parent_id)
            if parent is None:
                # Defensive: if the parent has already been removed (shouldn't
                # happen in normal flow) silently ignore the sub-step.
                return ""
            self._counter += 1
            item_id = f"sub_{self._counter}"
            sub = ChecklistItem(item_id=item_id, name=name)
            parent.substeps.append(sub)
            return item_id

    def ensure_substeps(self, parent_id: str, names: Sequence[str]) -> None:
        """Ensure a fixed ordered set of named sub-steps exists under *parent_id*."""
        with self._lock:
            parent = self._items.get(parent_id)
            if parent is None:
                return

            existing = {sub.name: sub for sub in parent.substeps}
            ordered: list[ChecklistItem] = []
            for name in names:
                sub = existing.get(name)
                if sub is None:
                    self._counter += 1
                    sub = ChecklistItem(item_id=f"sub_{self._counter}", name=name)
                ordered.append(sub)
            parent.substeps = ordered

    def complete_substep(self, item_id: str, *, detail: str = "") -> None:
        """Complete a previously-added sub-step."""
        self._set_status(item_id, Status.DONE, detail=detail)

    def warn_substep(self, item_id: str, *, detail: str = "") -> None:
        """Mark a sub-step as warning."""
        self._set_status(item_id, Status.WARN, detail=detail)

    def items(self) -> list[ChecklistItem]:
        """Return a snapshot of all top-level items in order."""
        with self._lock:
            return [self._items[i] for i in self._order]

    def append_log(self, message: str) -> None:
        """Append a line to the live verbose-log pane."""
        line = message.strip()
        if not line:
            return
        with self._lock:
            self._log_lines.append(line)

    def set_substep_status(self, parent_id: str, name: str, status: str, *, detail: str = "") -> None:
        """Set the status of a named sub-step under *parent_id*."""
        with self._lock:
            parent = self._items.get(parent_id)
            if parent is None:
                return
            for sub in parent.substeps:
                if sub.name == name:
                    sub.status = status
                    if status == Status.RUNNING and sub.spinner is None:
                        sub.spinner = Spinner("dots", style="cyan")
                    if detail:
                        sub.detail = detail
                    return

    def reset_substeps(self, parent_id: str) -> None:
        """Reset all sub-steps for *parent_id* back to the pending state."""
        with self._lock:
            parent = self._items.get(parent_id)
            if parent is None:
                return
            for sub in parent.substeps:
                sub.status = Status.PENDING
                sub.detail = ""

    # ── Internals ─────────────────────────────────────────────────────────

    def _set_status(self, item_id: str, status: str, *, detail: str = "") -> None:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                # Could be a sub-step — search substeps of all parents.
                for parent in self._items.values():
                    for sub in parent.substeps:
                        if sub.item_id == item_id:
                            sub.status = status
                            if status == Status.RUNNING and sub.spinner is None:
                                sub.spinner = Spinner("dots", style="cyan")
                            if detail:
                                sub.detail = detail
                            return
                return  # silently ignore unknown ids
            item.status = status
            if status == Status.RUNNING and item.spinner is None:
                item.spinner = Spinner("dots", style="cyan")
            if detail:
                item.detail = detail

    def __rich__(self) -> RenderableType:
        """Render the checklist as a Rich renderable."""
        rows: list[RenderableType] = []
        visible_items = self._visible_items()
        log_panel = self._log_panel()
        if log_panel is not None:
            rows.append(log_panel)
        if self._title:
            rows.append(Text(self._title, style="bold cyan"))
        for item in visible_items:
            rows.append(_render_item(item, indent=False))
            for sub in item.substeps:
                rows.append(_render_item(sub, indent=True))
        return Group(*rows) if rows else Text("")

    def _visible_items(self) -> list[ChecklistItem]:
        """Return only the rows that should be visible in the live region.

        Future pending steps are hidden until they become active so the live
        output stays compact while long-running fetch work is in progress.
        """
        items = self.items()
        if not items:
            return []

        last_active_idx = max(
            (idx for idx, item in enumerate(items) if item.status != Status.PENDING),
            default=-1,
        )
        if last_active_idx == -1:
            return items[:1]
        return items[: last_active_idx + 1]

    def _log_panel(self) -> RenderableType | None:
        """Return a compact live panel of recent verbose log lines."""
        with self._lock:
            if not self._log_lines:
                return None
            lines = list(self._log_lines)

        content = Text("\n").join(Text(line, style="dim") for line in lines)
        return Panel(content, title="Verbose logs", border_style="dim", padding=(0, 1), expand=False)


def _render_item(item: ChecklistItem, *, indent: bool) -> RenderableType:
    glyph = item.spinner if item.status == Status.RUNNING and item.spinner is not None else _glyph(item.status)
    name_style = "" if item.status != Status.PENDING else "dim"
    name = Text(item.name, style=name_style)
    if item.detail:
        name.append(f"  {item.detail}", style="dim")
    grid = Table.grid(padding=(0, 1))
    if indent:
        grid.add_column(width=7, no_wrap=True)
        grid.add_column(width=1, no_wrap=True)
        grid.add_column()
        grid.add_row(Text("    └─", style="dim"), glyph, name)
        return grid
    grid.add_column(width=1, no_wrap=True)
    grid.add_column()
    grid.add_row(glyph, name)
    return grid


# ── Verbose loguru handler ──────────────────────────────────────────────────
#
# Patterns are matched against the formatted ``record.message``. We only
# fire on records emitted from modules under ``atlantis.fetchers.*`` so
# we never pick up unrelated loguru output.


# Map of regex pattern → human-readable sub-step label. Patterns are
# evaluated in order; the first match wins. Keep these conservative:
# false positives are worse than missing a tick.
_SUBSTEP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Mosaicked .* tile\(s\)"), "Mosaic tiles"),
    (re.compile(r"Clipped to AOI"), "Clip to AOI"),
    (re.compile(r"Classification: flood"), "Classify pixels"),
    (re.compile(r"HDF4 subdataset for"), "Open HDF4"),
    (re.compile(r"Item \d+/\d+ processed"), "Process STAC item"),
    (re.compile(r"Replacing NaN with nodata"), "Fill nodata"),
)


class LoguruChecklistHandler(logging.Handler):
    """Routing loguru handler that surfaces matched messages as sub-steps.

    Loguru does not use the stdlib ``logging`` module directly, but it
    can be configured to call a sink. This handler implements the sink
    interface (``write`` + ``flush``) and routes every record to
    :meth:`emit` after parse. The handler itself is installed via
    :func:`loguru_logger.add` and receives the formatted message
    through ``record.message``.
    """

    def __init__(self, checklist: Checklist, *, current_parent: ContextVar[str | None]) -> None:
        """Bind the handler to a checklist and the active parent-step context."""
        super().__init__()
        self._checklist = checklist
        self._parent = current_parent
        # Cache (label → item_id) so we don't add the same sub-step twice.
        self._active: dict[str, str] = {}

    def emit(self, record: logging.LogRecord) -> None:
        """Stdlib ``logging`` API — not used by loguru, but kept for parity."""
        msg = record.getMessage()
        self._route(msg)

    def write(self, message: str) -> None:
        """Loguru sink: called with the formatted message."""
        # Strip a trailing newline that loguru appends.
        msg = message.rstrip("\n")
        self._route(msg)

    def flush(self) -> None:  # noqa: D401 - loguru sink requirement
        """Loguru sink no-op."""

    def _route(self, msg: str) -> None:
        parent_id = self._parent.get()
        if parent_id is None:
            return
        self._checklist.append_log(msg)
        profile = _current_profile.get()
        if profile is not None and self._advance_profile(profile, parent_id, msg):
            return
        for pattern, label in _SUBSTEP_PATTERNS:
            if pattern.search(msg):
                item_id = self._active.get(label)
                if item_id is None:
                    item_id = self._checklist.add_substep(parent_id, label)
                    self._active[label] = item_id
                # Mark complete on every matching message — many steps emit
                # multiple times (e.g. one per mosaic). Completing each
                # time is harmless and avoids stale "running" rows.
                self._checklist.complete_substep(item_id, detail="")
                return

    def _advance_profile(self, profile: ProfileBinding, parent_id: str, msg: str) -> bool:
        """Advance fixed sub-step rows for a known animation profile."""
        if profile.name not in _SUBSTEP_PROFILES:
            return False

        if re.search(r"Search complete: \d+ result\(s\) across \d+ date\(s\)", msg):
            if profile.pre_item_id is not None:
                self._checklist.complete(profile.pre_item_id)
            return True

        if re.search(r"Processing date \d{8}:", msg):
            if profile.pre_item_id is not None:
                self._checklist.complete(profile.pre_item_id)
            self._checklist.start(profile.process_item_id)
            self._checklist.ensure_substeps(profile.process_item_id, _SUBSTEP_PROFILES[profile.name])
            self._checklist.reset_substeps(profile.process_item_id)
            self._checklist.set_substep_status(profile.process_item_id, "Mosaic tiles", Status.RUNNING)
            return True

        if re.search(r"Mosaicked .* tile\(s\)", msg):
            self._checklist.set_substep_status(profile.process_item_id, "Mosaic tiles", Status.DONE)
            self._checklist.set_substep_status(profile.process_item_id, "Clip to AOI", Status.RUNNING)
            return True

        if re.search(r"Clipped to AOI", msg):
            self._checklist.set_substep_status(profile.process_item_id, "Clip to AOI", Status.DONE)
            self._checklist.set_substep_status(profile.process_item_id, "Classify pixels", Status.RUNNING)
            return True

        if re.search(r"Classification: flood", msg):
            self._checklist.set_substep_status(profile.process_item_id, "Classify pixels", Status.DONE)
            return True

        return False


# ── Context managers ────────────────────────────────────────────────────────
#
# ``_current_parent`` is a ContextVar so the loguru handler can find
# which row to attach sub-steps to without being threaded through every
# processor signature.

_current_parent: ContextVar[str | None] = ContextVar("atlantis_checklist_parent", default=None)
_checklist_live_active: ContextVar[bool] = ContextVar("atlantis_checklist_live_active", default=False)
_current_profile: ContextVar[ProfileBinding | None] = ContextVar("atlantis_checklist_profile", default=None)


def is_task_checklist_active() -> bool:
    """Return ``True`` when execution is inside a live checklist context."""
    return _checklist_live_active.get()


@contextmanager
def task_checklist(
    steps: Sequence[str],
    *,
    title: str | None = None,
    verbose: bool = False,
    console: Console | None = None,
) -> Iterator["_ChecklistHandle"]:
    """Yield a handle for running *steps* inside a live-updating checklist.

    Args:
        steps: Ordered step names to pre-register as pending rows.
        title: Optional bold title printed above the checklist.
        verbose: When ``True``, install a loguru handler that surfaces
            known processor debug strings as nested sub-steps.
        console: Rich console to render against. Defaults to the shared
            ``utils.ui.console``.

    Usage::

        with task_checklist(["Fetch tiles", "Plot", "Harmonise"]) as cl:
            with cl.step("Fetch tiles"):
                fetcher.fetch(...)
            with cl.step("Plot"):
                _plot_source(...)
            with cl.step("Harmonise"):
                _harmonise_source(...)
    """
    con = console or shared_console
    checklist = Checklist(title=title)
    for name in steps:
        checklist.add(name)

    handler: LoguruChecklistHandler | None = None
    handler_id: int | None = None

    if verbose:
        # Lazy import: loguru is a runtime dep but the rest of the
        # module is import-safe without it for tests.
        from loguru import logger as _logger

        handler = LoguruChecklistHandler(checklist, current_parent=_current_parent)
        handler_id = _logger.add(
            handler.write,
            level="DEBUG",
            format="{time:HH:mm:ss} | {message}",
            filter=lambda record: record["name"].startswith("atlantis"),
        )

    handle = _ChecklistHandle(checklist, con)
    active_token = _checklist_live_active.set(True)
    try:
        with Live(checklist, console=con, refresh_per_second=8, transient=False):
            yield handle
    finally:
        _checklist_live_active.reset(active_token)
        if handler_id is not None:
            from loguru import logger as _logger

            _logger.remove(handler_id)


class _ChecklistHandle:
    """Handle yielded by :func:`task_checklist`.

    Exposes :meth:`step` as a context manager that flips a row's status
    on enter / exit. Exceptions inside the ``with`` block mark the row
    as failed and re-raise.
    """

    def __init__(self, checklist: Checklist, console: Console) -> None:
        self._checklist = checklist
        self._console = console
        # Map step name → item_id for O(1) lookup inside ``step()``.
        self._ids: dict[str, str] = {}
        for item in checklist.items():
            self._ids[item.name] = item.item_id

    @contextmanager
    def step(
        self,
        name: str,
        *,
        profile: str | None = None,
        pre_step: str | None = None,
    ) -> Iterator[ChecklistItem]:
        """Run a block as *name*. Marks ✓ on success, ✗ on exception."""
        item_id = self._ids.get(name)
        if item_id is None:
            # Defensive: caller passed an unregistered step name.
            # Yield a dummy item so the with-block doesn't blow up.
            dummy = ChecklistItem(item_id="__unknown__", name=name)
            yield dummy
            return

        pre_item_id = self._ids.get(pre_step) if pre_step is not None else None
        token = _current_parent.set(item_id)
        binding = ProfileBinding(profile, item_id, pre_item_id) if profile is not None else None
        profile_token = _current_profile.set(binding)
        if profile in _SUBSTEP_PROFILES:
            self._checklist.ensure_substeps(item_id, _SUBSTEP_PROFILES[profile])
        if pre_item_id is not None:
            self._checklist.start(pre_item_id)
        else:
            self._checklist.start(item_id)
        try:
            yield self._checklist._items[item_id]
        except BaseException as exc:
            if pre_item_id is not None and self._checklist._items[item_id].status == Status.PENDING:
                self._checklist.fail(pre_item_id, detail=str(exc))
            else:
                self._checklist.fail(item_id, detail=str(exc))
            raise
        else:
            if pre_item_id is None:
                self._checklist.complete(item_id)
            elif self._checklist._items[item_id].status == Status.RUNNING:
                self._checklist.complete(item_id)
        finally:
            _current_profile.reset(profile_token)
            _current_parent.reset(token)

    def warn(self, name: str, *, detail: str = "") -> None:
        """Externally mark *name* as warned (use after the step's with-block)."""
        item_id = self._ids.get(name)
        if item_id is not None:
            self._checklist.warn(item_id, detail=detail)

    def fail(self, name: str, *, detail: str = "") -> None:
        """Externally mark *name* as failed (use after the step's with-block)."""
        item_id = self._ids.get(name)
        if item_id is not None:
            self._checklist.fail(item_id, detail=detail)

    def complete(self, name: str, *, detail: str = "") -> None:
        """Externally mark *name* as completed (use after the step's with-block)."""
        item_id = self._ids.get(name)
        if item_id is not None:
            self._checklist.complete(item_id, detail=detail)
