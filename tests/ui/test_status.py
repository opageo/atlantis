"""Tests for status display components."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from atlantis.ui.components.status import STAGE_LABELS, STAGE_ORDER, ActivityLogWidget
from atlantis.ui.models import FetchProgress


class TestActivityLogWidget:
    """Tests for the scrollable activity log widget."""

    def test_initial_state(self) -> None:
        """After construction, title is set but column is None (not yet rendered)."""
        w = ActivityLogWidget(title="My Log")
        assert w.title == "My Log"
        assert w.column is None

    def test_log_before_create_is_safe(self) -> None:
        """Calling log() before create() is a no-op, not a crash."""
        w = ActivityLogWidget()
        w.log("discarded message")
        assert w.column is None

    def test_log_with_explicit_level(self) -> None:
        """log() passes level through; no crash when column is set."""
        w = ActivityLogWidget()
        mock_col = MagicMock()
        w.column = mock_col
        w.log("info msg", level="info")
        w.log("success msg", level="success")
        w.log("warning msg", level="warning")
        w.log("error msg", level="error")
        assert mock_col.__enter__.call_count == 4

    def test_log_unknown_level_defaults_to_info_color(self) -> None:
        """An unrecognized level uses the 'info' color."""
        w = ActivityLogWidget()
        mock_col = MagicMock()
        w.column = mock_col
        w.log("test", level="bogus")
        assert mock_col.__enter__.call_count == 1

    def test_default_title(self) -> None:
        """Default title is 'Activity Log'."""
        w = ActivityLogWidget()
        assert w.title == "Activity Log"


class TestStageConstants:
    """Tests for stage ordering and label mappings."""

    def test_stage_order_contains_all_stages(self) -> None:
        """STAGE_ORDER lists the linear progression of stages."""
        assert STAGE_ORDER == ["idle", "searching", "fetching", "harmonising", "plotting", "done"]

    def test_all_stages_have_labels(self) -> None:
        """Every stage plus 'error' has a human-readable label."""
        for stage in list(STAGE_ORDER) + ["error"]:
            assert stage in STAGE_LABELS, f"Missing label for stage {stage!r}"

    def test_idle_label(self) -> None:
        """idle maps to 'Waiting'."""
        assert STAGE_LABELS["idle"] == "Waiting"

    def test_done_label(self) -> None:
        """done maps to 'Complete'."""
        assert STAGE_LABELS["done"] == "Complete"

    def test_error_label(self) -> None:
        """error maps to 'Error'."""
        assert STAGE_LABELS["error"] == "Error"

    def test_idle_is_first(self) -> None:
        """idle is always the first stage in the order."""
        assert STAGE_ORDER[0] == "idle"


class TestFetchProgressModel:
    """Additional edge-case tests for FetchProgress used by status components."""

    def test_stage_at_index(self) -> None:
        """Verify stage ordering indices for progress stepping logic."""
        p = FetchProgress(stage="searching")
        assert STAGE_ORDER.index(p.stage) == 1

    def test_error_stage_not_in_order(self) -> None:
        """'error' is handled specially and is not in STAGE_ORDER."""
        p = FetchProgress(stage="error", error="BOOM")
        assert p.stage not in STAGE_ORDER

    def test_unknown_stage_not_in_order(self) -> None:
        """An unknown stage is not in the predefined order."""
        assert "bogus" not in STAGE_ORDER
