"""Fetchers for various flood data sources."""

from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult, SearchResult
from atlantis.fetchers.gfm import GFMFetcher
from atlantis.fetchers.gfm.backend import GfmStacBackend
from atlantis.fetchers.gfm.processor import GfmRasterProcessor
from atlantis.fetchers.modis import MODISFetcher
from atlantis.fetchers.modis.backend import (
    LaadsHdf4Backend,
    LanceGeotiffBackend,
    ModisBackend,
)
from atlantis.fetchers.modis.processor import ModisRasterProcessor
from atlantis.fetchers.registry import fetcher_registry, get_fetcher, list_fetchers, register_fetcher
from atlantis.fetchers.viirs import VIIRSFetcher
from atlantis.fetchers.viirs.backend import (
    GmuLegacyBackend,
    ListingLocation,
    NoaaS3Backend,
    ViirsBackend,
    get_backend,
    list_backends,
)
from atlantis.fetchers.viirs.processor import ViirsRasterProcessor

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
    # GFM components
    "GFMFetcher",
    "GfmStacBackend",
    "GfmRasterProcessor",
    # VIIRS components
    "VIIRSFetcher",
    "ViirsBackend",
    "ListingLocation",
    "NoaaS3Backend",
    "GmuLegacyBackend",
    "get_backend",
    "list_backends",
    "ViirsRasterProcessor",
    # MODIS components
    "MODISFetcher",
    "ModisBackend",
    "LanceGeotiffBackend",
    "LaadsHdf4Backend",
    "ModisRasterProcessor",
]
