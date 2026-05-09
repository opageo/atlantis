"""Tests for FloodEvent model."""

from datetime import date

import pytest

from atlantis.models import FloodEvent


class TestFloodEvent:
    """Tests for FloodEvent dataclass."""

    def test_valid_event(self):
        """Test creating a valid FloodEvent."""
        event = FloodEvent(
            event_id="Valencia_2024",
            bbox=(-0.5, 39.2, 0.0, 39.8),
            start_date=date(2024, 10, 29),
            end_date=date(2024, 11, 5),
            sources=["gfm", "viirs"],
        )
        assert event.event_id == "Valencia_2024"
        assert event.bbox == (-0.5, 39.2, 0.0, 39.8)
        assert event.start_date == date(2024, 10, 29)
        assert event.end_date == date(2024, 11, 5)
        assert event.sources == ["gfm", "viirs"]

    def test_default_sources_empty(self):
        """Test that sources defaults to empty list."""
        event = FloodEvent(
            event_id="Test",
            bbox=(0, 0, 1, 1),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 2),
        )
        assert event.sources == []

    def test_invalid_longitude(self):
        """Test that longitude outside -180 to 180 raises ValueError."""
        with pytest.raises(ValueError, match="Longitude values"):
            FloodEvent(
                event_id="Test",
                bbox=(-200, 0, 1, 1),
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 2),
            )

    def test_invalid_latitude(self):
        """Test that latitude outside -90 to 90 raises ValueError."""
        with pytest.raises(ValueError, match="Latitude values"):
            FloodEvent(
                event_id="Test",
                bbox=(0, -100, 1, 1),
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 2),
            )

    def test_west_greater_than_east(self):
        """Test that west > east raises ValueError."""
        with pytest.raises(ValueError, match="West longitude must be <="):
            FloodEvent(
                event_id="Test",
                bbox=(1, 0, 0, 1),
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 2),
            )

    def test_south_greater_than_north(self):
        """Test that south > north raises ValueError."""
        with pytest.raises(ValueError, match="South latitude must be <="):
            FloodEvent(
                event_id="Test",
                bbox=(0, 1, 1, 0),
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 2),
            )

    def test_end_date_before_start_date(self):
        """Test that end_date < start_date raises ValueError."""
        with pytest.raises(ValueError, match="end_date must be >="):
            FloodEvent(
                event_id="Test",
                bbox=(0, 0, 1, 1),
                start_date=date(2024, 1, 5),
                end_date=date(2024, 1, 1),
            )
