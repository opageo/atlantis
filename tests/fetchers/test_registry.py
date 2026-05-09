"""Tests for fetcher registry."""

import pytest

from atlantis.fetchers import fetcher_registry, get_fetcher, list_fetchers
from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult, SearchResult
from atlantis.fetchers.registry import register_fetcher
from atlantis.models import FloodEvent


class TestFetcherRegistry:
    """Tests for fetcher registry functionality."""

    def test_list_fetchers_after_import(self):
        """Test list_fetchers contains registered fetchers after imports."""
        # Fetchers are registered at import time in cli.py
        from atlantis.fetchers import gfm, rfm, viirs  # noqa: F401

        fetchers = list_fetchers()
        assert len(fetchers) >= 3
        assert "gfm" in fetchers
        assert "viirs" in fetchers
        assert "rfm" in fetchers

    def test_register_fetcher(self):
        """Test registering a fetcher."""

        @register_fetcher("test_fetcher")
        class TestFetcher(AbstractFloodFetcher):
            source_id = "test_fetcher"

            def search(self, event: FloodEvent) -> list[SearchResult]:
                return []

            def fetch(self, event: FloodEvent, output_dir) -> list[FetchResult]:
                return []

        assert "test_fetcher" in list_fetchers()
        assert fetcher_registry["test_fetcher"] == TestFetcher

    def test_register_duplicate_raises(self):
        """Test that registering same fetcher twice raises ValueError."""

        @register_fetcher("dup_test")
        class TestFetcher1(AbstractFloodFetcher):
            source_id = "dup_test"

            def search(self, event: FloodEvent) -> list[SearchResult]:
                return []

            def fetch(self, event: FloodEvent, output_dir) -> list[FetchResult]:
                return []

        with pytest.raises(ValueError, match="already registered"):

            @register_fetcher("dup_test")
            class TestFetcher2(AbstractFloodFetcher):
                source_id = "dup_test"

                def search(self, event: FloodEvent) -> list[SearchResult]:
                    return []

                def fetch(self, event: FloodEvent, output_dir) -> list[FetchResult]:
                    return []

    def test_get_fetcher(self):
        """Test getting a registered fetcher by name."""

        @register_fetcher("my_fetcher")
        class MyFetcher(AbstractFloodFetcher):
            source_id = "my_fetcher"

            def search(self, event: FloodEvent) -> list[SearchResult]:
                return []

            def fetch(self, event: FloodEvent, output_dir) -> list[FetchResult]:
                return []

        fetcher_cls = get_fetcher("my_fetcher")
        assert fetcher_cls == MyFetcher

    def test_get_fetcher_not_found(self):
        """Test that getting unregistered fetcher raises KeyError."""
        with pytest.raises(KeyError, match="not found"):
            get_fetcher("nonexistent")

    def test_gfm_viirs_rfm_fetcher_registered(self):
        """Test that GFM, VIIRS, and RFM fetchers are registered."""
        # Import to trigger registration
        from atlantis.fetchers import gfm, rfm, viirs  # noqa: F401

        assert "gfm" in list_fetchers()
        assert "viirs" in list_fetchers()
        assert "rfm" in list_fetchers()
