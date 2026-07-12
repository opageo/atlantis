"""Tests for CLI UI utility functions."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from atlantis.utils.ui import (
    fail,
    info,
    ok,
    skip,
    step_status,
    warn,
)


class TestStatusGlyphs:
    """Tests for CLI status line printers."""

    def test_ok_calls_console_print(self) -> None:
        mock_console = MagicMock()
        with patch("atlantis.utils.ui.console", mock_console):
            ok("done")
        mock_console.print.assert_called_once()
        assert "done" in mock_console.print.call_args[0][0]

    def test_warn_calls_console_print(self) -> None:
        mock_console = MagicMock()
        with patch("atlantis.utils.ui.console", mock_console):
            warn("careful")
        mock_console.print.assert_called_once()
        assert "careful" in mock_console.print.call_args[0][0]

    def test_fail_calls_console_print(self) -> None:
        mock_console = MagicMock()
        with patch("atlantis.utils.ui.console", mock_console):
            fail("error")
        mock_console.print.assert_called_once()
        assert "error" in mock_console.print.call_args[0][0]

    def test_info_calls_console_print(self) -> None:
        mock_console = MagicMock()
        with patch("atlantis.utils.ui.console", mock_console):
            info("note")
        mock_console.print.assert_called_once()
        assert "note" in mock_console.print.call_args[0][0]

    def test_skip_calls_console_print(self) -> None:
        mock_console = MagicMock()
        with patch("atlantis.utils.ui.console", mock_console):
            skip("skipped")
        mock_console.print.assert_called_once()
        assert "skipped" in mock_console.print.call_args[0][0]


class TestStepStatus:
    """Tests for the step_status spinner context manager."""

    def test_context_manager_yields(self) -> None:
        mock_console = MagicMock()
        with patch("atlantis.utils.ui.console", mock_console):
            with step_status("loading"):
                pass
        mock_console.status.assert_called_once_with("loading", spinner="dots")

    def test_nested_block_runs(self) -> None:
        mock_console = MagicMock()
        side_effect = []
        with patch("atlantis.utils.ui.console", mock_console):
            with step_status("processing"):
                side_effect.append("ran")
        assert side_effect == ["ran"]
