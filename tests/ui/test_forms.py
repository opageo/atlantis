"""Tests for form components."""

from __future__ import annotations

import pytest

from atlantis.ui.components.forms import EVENT_PRESETS


class TestEventPresets:
    """Tests for the built-in event preset data."""

    def test_presets_is_list(self) -> None:
        """EVENT_PRESETS is a list of dicts."""
        assert isinstance(EVENT_PRESETS, list)
        assert len(EVENT_PRESETS) > 0
        for p in EVENT_PRESETS:
            assert isinstance(p, dict)

    def test_required_keys_present(self) -> None:
        """Every preset has name, event_id, bbox, date_from, date_to."""
        for preset in EVENT_PRESETS:
            for key in ("name", "event_id", "bbox", "date_from", "date_to"):
                assert key in preset, f"Missing key {key!r} in {preset['name']}"

    def test_bbox_is_four_tuple(self) -> None:
        """Every bbox is a 4-element tuple."""
        for preset in EVENT_PRESETS:
            bbox = preset["bbox"]
            assert isinstance(bbox, tuple), f"bbox is not a tuple in {preset['name']}"
            assert len(bbox) == 4, f"bbox length != 4 in {preset['name']}"

    def test_bbox_coordinates_valid(self) -> None:
        """Bbox coordinates are in valid ranges."""
        for preset in EVENT_PRESETS:
            west, south, east, north = preset["bbox"]
            assert -180 <= west <= 180, f"west {west} out of range in {preset['name']}"
            assert -180 <= east <= 180, f"east {east} out of range in {preset['name']}"
            assert -90 <= south <= 90, f"south {south} out of range in {preset['name']}"
            assert -90 <= north <= 90, f"north {north} out of range in {preset['name']}"

    def test_dates_are_YYYY_MM_DD(self) -> None:
        """Dates follow the 'YYYY-MM-DD' format."""
        import re

        date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for preset in EVENT_PRESETS:
            assert date_re.match(preset["date_from"]), f"date_from {preset['date_from']!r} invalid in {preset['name']}"
            assert date_re.match(preset["date_to"]), f"date_to {preset['date_to']!r} invalid in {preset['name']}"

    def test_date_from_before_date_to(self) -> None:
        """start date is not after end date."""
        for preset in EVENT_PRESETS:
            assert preset["date_from"] <= preset["date_to"], f"date_from > date_to in {preset['name']}"

    def test_known_presets(self) -> None:
        """The expected event names are present."""
        names = {p["name"] for p in EVENT_PRESETS}
        assert "Valencia 2024" in names
        assert "Harvey 2017" in names
        assert "Bihar 2019" in names

    def test_event_ids_are_unique(self) -> None:
        """No two presets share the same event_id."""
        ids = [p["event_id"] for p in EVENT_PRESETS]
        assert len(ids) == len(set(ids))

    @pytest.mark.parametrize(
        "name",
        ["Valencia 2024", "Harvey 2017", "Bihar 2019", "Vamco 2020", "West Africa 2020"],
    )
    def test_each_preset_has_valid_data(self, name: str) -> None:
        """Each named preset passes basic validation."""
        preset = next(p for p in EVENT_PRESETS if p["name"] == name)
        assert len(preset["bbox"]) == 4
        assert preset["date_from"] <= preset["date_to"]
