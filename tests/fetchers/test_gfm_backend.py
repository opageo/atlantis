"""Tests for the GFM STAC backend."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from atlantis.fetchers.gfm.backend import (
    DEFAULT_GFM_STAC_URL,
    GFM_COLLECTION_ID,
    GfmStacBackend,
)
from atlantis.models.event import FloodEvent

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def small_event():
    """Minimal flood event for backend tests."""
    from datetime import date

    return FloodEvent(
        event_id="test_event",
        bbox=(10.0, 20.0, 11.0, 21.0),
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 3),
        sources=["gfm"],
    )


def _make_mock_item(item_id="item_001", dt=None, bbox=None, properties=None):
    """Return a minimal mock STAC item."""
    item = MagicMock()
    item.id = item_id
    item.datetime = dt or datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc)
    item.bbox = bbox or [10.0, 20.0, 11.0, 21.0]
    item.properties = properties or {}
    return item


# ── GfmStacBackend construction ───────────────────────────────────────────────


class TestGfmStacBackendInit:
    def test_defaults(self):
        backend = GfmStacBackend()
        assert backend.api_url == DEFAULT_GFM_STAC_URL
        assert backend.collection_id == GFM_COLLECTION_ID
        assert backend.max_items == 1000

    def test_custom_params(self):
        backend = GfmStacBackend(
            api_url="https://example.com/stac",
            collection_id="CUSTOM_COLLECTION",
            max_items=500,
        )
        assert backend.api_url == "https://example.com/stac"
        assert backend.collection_id == "CUSTOM_COLLECTION"
        assert backend.max_items == 500


# ── group_items_by_date ────────────────────────────────────────────────────────


class TestGroupItemsByDate:
    def test_single_item(self):
        item = _make_mock_item(dt=datetime(2024, 10, 30, 6, 0, tzinfo=timezone.utc))
        groups = GfmStacBackend.group_items_by_date([item])
        assert list(groups.keys()) == ["20241030"]
        assert groups["20241030"] == [item]

    def test_multiple_items_same_date(self):
        item_a = _make_mock_item("a", dt=datetime(2024, 10, 30, 6, 0, tzinfo=timezone.utc))
        item_b = _make_mock_item("b", dt=datetime(2024, 10, 30, 18, 0, tzinfo=timezone.utc))
        groups = GfmStacBackend.group_items_by_date([item_a, item_b])
        assert len(groups) == 1
        assert len(groups["20241030"]) == 2

    def test_items_on_different_dates(self):
        item_a = _make_mock_item("a", dt=datetime(2024, 10, 30, 6, 0, tzinfo=timezone.utc))
        item_b = _make_mock_item("b", dt=datetime(2024, 10, 31, 6, 0, tzinfo=timezone.utc))
        groups = GfmStacBackend.group_items_by_date([item_a, item_b])
        assert set(groups.keys()) == {"20241030", "20241031"}
        assert len(groups["20241030"]) == 1
        assert len(groups["20241031"]) == 1

    def test_empty_collection(self):
        groups = GfmStacBackend.group_items_by_date([])
        assert groups == {}

    def test_item_without_datetime_uses_properties_fallback(self):
        """Item with no .datetime attribute but datetime in .properties is grouped."""
        item = MagicMock()
        item.id = "no_dt_item"
        item.datetime = None
        item.properties = {"datetime": "2024-03-15T08:00:00Z"}
        groups = GfmStacBackend.group_items_by_date([item])
        assert "20240315" in groups

    def test_item_without_datetime_and_no_properties_is_skipped(self):
        """Item with no datetime at all is skipped with a warning."""
        item = MagicMock()
        item.id = "broken_item"
        item.datetime = None
        item.properties = {}
        groups = GfmStacBackend.group_items_by_date([item])
        assert groups == {}

    def test_mixed_items_some_missing_datetime(self):
        """Items with valid datetimes are still grouped even if others are broken."""
        valid = _make_mock_item("valid", dt=datetime(2024, 1, 1, tzinfo=timezone.utc))
        broken = MagicMock()
        broken.id = "broken"
        broken.datetime = None
        broken.properties = {}
        groups = GfmStacBackend.group_items_by_date([valid, broken])
        assert list(groups.keys()) == ["20240101"]
        assert len(groups["20240101"]) == 1


# ── search() (mocked network) ─────────────────────────────────────────────────


class TestGfmStacBackendSearch:
    def _make_mock_catalog(self, items):
        """Build a mock pystac_client.Client.open() chain."""
        mock_search_result = MagicMock()
        mock_search_result.item_collection.return_value = items

        mock_catalog = MagicMock()
        mock_catalog.search.return_value = mock_search_result

        return mock_catalog

    def test_search_returns_items(self, small_event):
        items = [_make_mock_item("i1"), _make_mock_item("i2")]
        mock_catalog = self._make_mock_catalog(items)

        with patch("pystac_client.Client.open", return_value=mock_catalog):
            backend = GfmStacBackend()
            result = backend.search(small_event)

        assert len(result) == 2

    def test_search_calls_correct_collection(self, small_event):
        mock_catalog = self._make_mock_catalog([])

        with patch("pystac_client.Client.open", return_value=mock_catalog):
            backend = GfmStacBackend()
            backend.search(small_event)

        call_kwargs = mock_catalog.search.call_args.kwargs
        assert call_kwargs["collections"] == GFM_COLLECTION_ID

    def test_search_passes_bbox_via_intersects(self, small_event):
        mock_catalog = self._make_mock_catalog([])

        with patch("pystac_client.Client.open", return_value=mock_catalog):
            backend = GfmStacBackend()
            backend.search(small_event)

        call_kwargs = mock_catalog.search.call_args.kwargs
        # intersects is a shapely geometry; check its bounds match the event bbox
        intersects = call_kwargs["intersects"]
        assert intersects.bounds == small_event.bbox

    def test_search_passes_date_range(self, small_event):
        mock_catalog = self._make_mock_catalog([])

        with patch("pystac_client.Client.open", return_value=mock_catalog):
            backend = GfmStacBackend()
            backend.search(small_event)

        call_kwargs = mock_catalog.search.call_args.kwargs
        start_dt, end_dt = call_kwargs["datetime"]
        assert start_dt.date() == small_event.start_date
        assert end_dt.date() == small_event.end_date

    def test_search_respects_max_items(self, small_event):
        mock_catalog = self._make_mock_catalog([])

        with patch("pystac_client.Client.open", return_value=mock_catalog):
            backend = GfmStacBackend(max_items=42)
            backend.search(small_event)

        call_kwargs = mock_catalog.search.call_args.kwargs
        assert call_kwargs["max_items"] == 42

    def test_search_empty_result(self, small_event):
        mock_catalog = self._make_mock_catalog([])

        with patch("pystac_client.Client.open", return_value=mock_catalog):
            backend = GfmStacBackend()
            result = backend.search(small_event)

        assert result is not None
        assert len(result) == 0

    def test_search_opens_correct_api_url(self, small_event):
        mock_catalog = self._make_mock_catalog([])

        with patch("pystac_client.Client.open", return_value=mock_catalog) as mock_open:
            backend = GfmStacBackend(api_url="https://custom.stac.example/api/v1")
            backend.search(small_event)

        mock_open.assert_called_once_with("https://custom.stac.example/api/v1")
