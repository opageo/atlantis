"""Tests for the RFM fetcher."""

from datetime import date
from pathlib import Path

import pytest

from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult
from atlantis.fetchers.registry import fetcher_registry
from atlantis.fetchers.rfm import RFMFetcher
from atlantis.models.event import FloodEvent


class TestRFMFetcherStructure:
    """Verify RFMFetcher satisfies the fetcher protocol/ABC contract."""

    def test_is_abstract_flood_fetcher(self):
        assert issubclass(RFMFetcher, AbstractFloodFetcher)

    def test_has_source_id(self):
        assert RFMFetcher.source_id == "rfm"

    def test_instantiation_default(self):
        fetcher = RFMFetcher()
        assert fetcher.api_url is None

    def test_instantiation_custom_url(self):
        fetcher = RFMFetcher(api_url="https://rfm.api.example.com")
        assert fetcher.api_url == "https://rfm.api.example.com"

    def test_registered_in_registry(self):
        assert "rfm" in fetcher_registry or issubclass(RFMFetcher, AbstractFloodFetcher)

    def test_is_protocol_compliant(self):
        fetcher = RFMFetcher()
        assert hasattr(fetcher, "search")
        assert hasattr(fetcher, "fetch")
        assert hasattr(fetcher, "to_dataset")
        assert callable(fetcher.search)
        assert callable(fetcher.fetch)
        assert callable(fetcher.to_dataset)


class TestRFMFetcherSearch:
    def test_search_returns_empty_list(self):
        fetcher = RFMFetcher()
        event = FloodEvent(
            event_id="test_event",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["rfm"],
        )
        results = fetcher.search(event)
        assert results == []

    def test_search_with_multi_day_event(self):
        fetcher = RFMFetcher()
        event = FloodEvent(
            event_id="test_event",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 5),
            sources=["rfm"],
        )
        results = fetcher.search(event)
        assert results == []


class TestRFMFetcherFetch:
    def test_fetch_returns_empty_list(self):
        fetcher = RFMFetcher()
        event = FloodEvent(
            event_id="test_event",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["rfm"],
        )
        results = fetcher.fetch(event, Path("/tmp/rfm_test"))
        assert results == []


class TestRFMFetcherToDataset:
    def test_to_dataset_raises_not_implemented(self):
        fetcher = RFMFetcher()

        dummy_result = FetchResult(
            event_id="test_event",
            source_id="rfm",
            files=[],
            metadata=type("M", (), {"event_id": "test_event", "source_id": "rfm"})(),
        )

        with pytest.raises(NotImplementedError, match="RFM to_dataset not yet implemented"):
            fetcher.to_dataset(dummy_result)
