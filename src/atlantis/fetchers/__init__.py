"""Fetchers for various flood data sources."""

from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult, SearchResult
from atlantis.fetchers.registry import fetcher_registry, get_fetcher, list_fetchers, register_fetcher
from atlantis.fetchers.viirs import VIIRSFetcher
from atlantis.fetchers.viirs_backend import (
    GmuLegacyBackend,
    ListingLocation,
    NoaaS3Backend,
    ViirsBackend,
    get_backend,
    list_backends,
)
from atlantis.fetchers.viirs_processor import ViirsRasterProcessor

__all__ = [
    # Base abstractions
    "AbstractFloodFetcher",
    "FetchResult",
    "SearchResult",
    # Registry
    "get_fetcher",
    "register_fetcher",
    "fetcher_registry",
    "list_fetchers",
    # VIIRS components
    "VIIRSFetcher",
    "ViirsBackend",
    "ListingLocation",
    "NoaaS3Backend",
    "GmuLegacyBackend",
    "get_backend",
    "list_backends",
    "ViirsRasterProcessor",
]
