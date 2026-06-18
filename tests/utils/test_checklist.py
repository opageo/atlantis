"""Tests for the live checklist CLI primitive."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from atlantis.utils.checklist import (
    Checklist,
    LoguruChecklistHandler,
    Status,
    _current_parent,
    is_task_checklist_active,
    task_checklist,
)


def _quiet_console() -> Console:
    """Return a non-TTY console to keep test output deterministic."""
    return Console(file=StringIO(), force_terminal=False, color_system=None)


def test_checklist_status_transitions() -> None:
    """Top-level items should expose stable status/detail transitions."""
    checklist = Checklist(title="Demo")
    fetch_id = checklist.add("Fetch tiles")
    plot_id = checklist.add("Plot")
    harm_id = checklist.add("Harmonise")

    checklist.start(fetch_id)
    checklist.complete(fetch_id)
    checklist.warn(plot_id, detail="skipped by flag")
    checklist.fail(harm_id, detail="disk full")

    items = checklist.items()
    assert [item.name for item in items] == ["Fetch tiles", "Plot", "Harmonise"]
    assert [item.status for item in items] == [Status.DONE, Status.WARN, Status.FAIL]
    assert items[1].detail == "skipped by flag"
    assert items[2].detail == "disk full"


def test_task_checklist_marks_step_complete() -> None:
    """A successful step context should end in the DONE state."""
    handle = None

    with task_checklist(["Fetch tiles", "Plot"], title="Smoke", console=_quiet_console()) as h:
        handle = h
        with h.step("Fetch tiles"):
            pass

    assert handle is not None
    items = handle._checklist.items()
    assert items[0].status == Status.DONE
    assert items[1].status == Status.PENDING


def test_running_step_keeps_persistent_spinner_instance() -> None:
    """Running rows should reuse the same Spinner object across renders."""
    console = _quiet_console()

    with task_checklist(["Fetch tiles"], console=console) as h:
        with h.step("Fetch tiles"):
            item = h._checklist.items()[0]
            first_spinner = item.spinner
            assert first_spinner is not None
            console.print(h._checklist)
            console.print(h._checklist)
            assert h._checklist.items()[0].spinner is first_spinner


def test_task_checklist_marks_step_failed_and_reraises() -> None:
    """Exceptions inside a step should mark FAIL and propagate."""
    handle = None

    with pytest.raises(ValueError, match="boom"):
        with task_checklist(["Fetch tiles"], title="Smoke", console=_quiet_console()) as h:
            handle = h
            with h.step("Fetch tiles"):
                raise ValueError("boom")

    assert handle is not None
    item = handle._checklist.items()[0]
    assert item.status == Status.FAIL
    assert item.detail == "boom"


def test_checklist_supports_nested_substeps() -> None:
    """Sub-steps should be attached to their parent row and independently tracked."""
    checklist = Checklist()
    fetch_id = checklist.add("Fetch tiles")

    sub_id = checklist.add_substep(fetch_id, "Mosaic tiles")
    checklist.complete_substep(sub_id, detail="4 inputs")

    item = checklist.items()[0]
    assert len(item.substeps) == 1
    assert item.substeps[0].name == "Mosaic tiles"
    assert item.substeps[0].status == Status.DONE
    assert item.substeps[0].detail == "4 inputs"


def test_loguru_handler_routes_known_messages_to_substeps() -> None:
    """Known processor messages should appear as completed nested rows."""
    handle = None
    handler = None

    with task_checklist(["Fetch tiles"], console=_quiet_console()) as h:
        handle = h
        handler = LoguruChecklistHandler(h._checklist, current_parent=_current_parent)
        with h.step("Fetch tiles"):
            handler.write("Mosaicked 4 tile(s) -> shape (512, 512)")
            handler.write("Clipped to AOI -> shape (128, 128)")
            handler.write("Classification: flood 12.3%, cloud 4.2%")

    assert handle is not None
    assert handler is not None
    substeps = handle._checklist.items()[0].substeps
    assert [sub.name for sub in substeps] == ["Mosaic tiles", "Clip to AOI", "Classify pixels"]
    assert [sub.status for sub in substeps] == [Status.DONE, Status.DONE, Status.DONE]


def test_loguru_handler_deduplicates_repeated_messages() -> None:
    """Repeated matches for the same label should not create duplicate rows."""
    handle = None
    handler = None

    with task_checklist(["Fetch tiles"], console=_quiet_console()) as h:
        handle = h
        handler = LoguruChecklistHandler(h._checklist, current_parent=_current_parent)
        with h.step("Fetch tiles"):
            handler.write("Mosaicked 2 tile(s) -> shape (64, 64)")
            handler.write("Mosaicked 3 tile(s) -> shape (64, 64)")

    assert handle is not None
    assert handler is not None
    substeps = handle._checklist.items()[0].substeps
    assert len(substeps) == 1
    assert substeps[0].name == "Mosaic tiles"
    assert substeps[0].status == Status.DONE


def test_profiled_substeps_animate_in_fixed_rows() -> None:
    """VIIRS/MODIS fetch profiles should keep fixed rows and advance one spinner at a time."""
    handler = None

    with task_checklist(["Fetch tiles", "Process tiles"], console=_quiet_console()) as h:
        handler = LoguruChecklistHandler(h._checklist, current_parent=_current_parent)
        with h.step("Process tiles", profile="viirs_fetch", pre_step="Fetch tiles"):
            items = h._checklist.items()
            assert items[0].status == Status.RUNNING
            assert items[1].status == Status.PENDING

            substeps = items[1].substeps
            assert [sub.name for sub in substeps] == ["Mosaic tiles", "Clip to AOI", "Classify pixels"]
            assert [sub.status for sub in substeps] == [Status.PENDING, Status.PENDING, Status.PENDING]

            handler.write("15:23:45 | Search complete: 14 result(s) across 7 date(s)")
            assert [item.status for item in items[:2]] == [Status.DONE, Status.PENDING]

            handler.write("15:23:45 | Processing date 20241104: 2 tile(s) (stream mode)")
            assert [item.status for item in items[:2]] == [Status.DONE, Status.RUNNING]
            assert [sub.status for sub in substeps] == [Status.RUNNING, Status.PENDING, Status.PENDING]

            handler.write("15:23:57 | Mosaicked 2 tile(s) -> shape (1, 4448, 8896)")
            assert [sub.status for sub in substeps] == [Status.DONE, Status.RUNNING, Status.PENDING]

            handler.write("15:23:57 | Clipped to AOI -> shape (1, 357, 594)")
            assert [sub.status for sub in substeps] == [Status.DONE, Status.DONE, Status.RUNNING]

            handler.write("15:23:57 | Classification: flood 0.2%, cloud 37.6%,")
            assert [sub.status for sub in substeps] == [Status.DONE, Status.DONE, Status.DONE]

            handler.write("15:23:58 | Processing date 20241105: 2 tile(s) (stream mode)")
            assert [sub.status for sub in substeps] == [Status.RUNNING, Status.PENDING, Status.PENDING]


def test_live_render_hides_trailing_pending_steps() -> None:
    """The live render should stay compact until later steps become active."""
    console = _quiet_console()
    checklist = Checklist(title="Demo")
    fetch_id = checklist.add("Fetch tiles")
    checklist.add("Plot outputs")
    checklist.add("Harmonise outputs")

    checklist.start(fetch_id)
    console.print(checklist)

    rendered = console.file.getvalue()
    assert "Fetch tiles" in rendered
    assert "Plot outputs" not in rendered
    assert "Harmonise outputs" not in rendered


def test_task_checklist_marks_live_region_active() -> None:
    """The checklist-active flag should be true only inside the live context."""
    assert is_task_checklist_active() is False

    with task_checklist(["Fetch tiles"], console=_quiet_console()):
        assert is_task_checklist_active() is True

    assert is_task_checklist_active() is False


def test_log_panel_renders_recent_verbose_lines() -> None:
    """Verbose log lines should render above the checklist rows."""
    console = _quiet_console()
    checklist = Checklist()
    fetch_id = checklist.add("Fetch tiles")
    checklist.start(fetch_id)
    checklist.append_log("15:23:45 | Found 145 entries in listing")
    checklist.append_log("15:23:48 | Date 20241104: 144 entries, 2 AOI matches")

    console.print(checklist)

    rendered = console.file.getvalue()
    assert "Verbose logs" in rendered
    assert "Found 145 entries in listing" in rendered
    assert "Date 20241104: 144 entries, 2 AOI matches" in rendered
