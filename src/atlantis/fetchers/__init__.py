"""Fetchers for various flood data sources."""

from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult, SearchResult
from atlantis.fetchers.registry import fetcher_registry, get_fetcher, list_fetchers, register_fetcher

__all__ = [
    "AbstractFloodFetcher",
    "FetchResult",
    "SearchResult",
    "get_fetcher",
    "register_fetcher",
    "fetcher_registry",
    "list_fetchers",
]
