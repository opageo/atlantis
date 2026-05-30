"""Tests for the GFM fetcher."""

from datetime import date
from pathlib import Path

import pytest

from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult
from atlantis.fetchers.gfm import GFMFetcher
from atlantis.fetchers.registry import fetcher_registry
from atlantis.models.event import FloodEvent


class TestGFMFetcherStructure:
    """Verify GFMFetcher satisfies the fetcher protocol/ABC contract."""

    def test_is_abstract_flood_fetcher(self):
        assert issubclass(GFMFetcher, AbstractFloodFetcher)

    def test_has_source_id(self):
        assert GFMFetcher.source_id == "gfm"

    def test_instantiation_default(self):
        fetcher = GFMFetcher()
        assert fetcher.api_url == "https://stac.eodc.eu/api/v1"

    def test_instantiation_custom_url(self):
        fetcher = GFMFetcher(api_url="https://custom.api/v1")
        assert fetcher.api_url == "https://custom.api/v1"

    def test_registered_in_registry(self):
        assert "gfm" in fetcher_registry or issubclass(GFMFetcher, AbstractFloodFetcher)

    def test_is_protocol_compliant(self):
        fetcher = GFMFetcher()
        assert hasattr(fetcher, "search")
        assert hasattr(fetcher, "fetch")
        assert hasattr(fetcher, "to_dataset")
        assert callable(fetcher.search)
        assert callable(fetcher.fetch)
        assert callable(fetcher.to_dataset)


class TestGFMFetcherSearch:
    def test_search_returns_empty_list(self):
        """Stub implementation returns empty list."""
        fetcher = GFMFetcher()
        event = FloodEvent(
            event_id="test_event",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["gfm"],
        )
        results = fetcher.search(event)
        assert results == []

    def test_search_returns_list_of_search_result(self):
        """Even the stub should return list type."""
        fetcher = GFMFetcher()
        event = FloodEvent(
            event_id="test_event",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 3),
            sources=["gfm"],
        )
        results = fetcher.search(event)
        assert isinstance(results, list)


class TestGFMFetcherFetch:
    def test_fetch_returns_empty_list(self):
        fetcher = GFMFetcher()
        event = FloodEvent(
            event_id="test_event",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["gfm"],
        )
        results = fetcher.fetch(event, Path("/tmp/gfm_test"))
        assert results == []


class TestGFMFetcherToDataset:
    def test_to_dataset_raises_not_implemented(self):
        fetcher = GFMFetcher()

        dummy_result = FetchResult(
            event_id="test_event",
            source_id="gfm",
            files=[],
            metadata=type("M", (), {"event_id": "test_event", "source_id": "gfm"})(),
        )

        with pytest.raises(NotImplementedError, match="GFM to_dataset not yet implemented"):
            fetcher.to_dataset(dummy_result)
