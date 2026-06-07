"""Backend abstractions for VIIRS data sources.

This module implements the Strategy pattern to encapsulate backend-specific
logic for different VIIRS data sources (NOAA S3, GMU Legacy, etc.).
"""

import re
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import requests
from loguru import logger

S3_NAMESPACE = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
NOAA_VIIRS_PREFIX = "JPSS_Blended_Products/VFM_1day_GLB"


@dataclass(frozen=True)
class ListingLocation:
    """Location information for directory/listing queries.

    Attributes:
        locator: The prefix or URL path for listing queries.
        date_token: The date string token used in item identification.
    """

    locator: str
    date_token: str


@dataclass(frozen=True)
class RemoteFile:
    """Information about a remote file found in a listing.

    Attributes:
        filename: The name of the file.
        url: The full URL to download the file.
    """

    filename: str
    url: str


class ViirsBackend(ABC):
    """Abstract base class for VIIRS data source backends.

    Implementations handle backend-specific concerns:
    - Directory/listing queries
    - Filename matching/parsing
    - URL construction
    """

    @abstractmethod
    def get_directory_links(self, base_url: str, location: str, timeout: int) -> list[str]:
        """Return entries from a directory listing.

        Args:
            base_url: The base URL for the backend.
            location: The listing location (prefix or path).
            timeout: Request timeout in seconds.

        Returns:
            List of entry identifiers (filenames, keys, or hrefs).
        """
        ...

    @abstractmethod
    def find_remote_filename(self, aoi_id: int, entries: list[str]) -> str | None:
        """Locate the matching filename for a date/AOI pair.

        Args:
            aoi_id: The AOI identifier.
            entries: List of entries from the directory listing.

        Returns:
            The matching filename, or None if not found.
        """
        ...

    @abstractmethod
    def get_listing_location(self, base_url: str, event_date: datetime, data_format: str) -> ListingLocation:
        """Return the listing location for a given date.

        Args:
            base_url: The base URL for the backend.
            event_date: The date to search for.
            data_format: The data format (e.g., "tif", "netcdf").

        Returns:
            ListingLocation with locator and date token.
        """
        ...

    def available_years(self, base_url: str, data_format: str, timeout: int) -> set[int] | None:
        """Return the set of calendar years for which this backend publishes data.

        The default implementation returns ``None``, meaning the backend does
        not declare its coverage and callers must attempt the listing for
        every requested date. Subclasses may override to enable cheap
        early-exit checks (e.g. a single ``ListBucket`` call for an S3 prefix)
        and produce clearer diagnostics when a request falls into a known
        coverage gap.

        Args:
            base_url: The base URL for the backend.
            data_format: The data format (e.g. ``"tif"``).
            timeout: Request timeout in seconds.

        Returns:
            A set of years (e.g. ``{2012, 2013, ..., 2026}``) when the backend
            can enumerate its coverage cheaply, or ``None`` when coverage is
            unknown and listings should be attempted unconditionally.
        """
        return None

    @abstractmethod
    def build_result_url(self, base_url: str, listing_location: str, filename: str) -> str:
        """Build the downloadable URL for a listing entry.

        Args:
            base_url: The base URL for the backend.
            listing_location: The listing location from get_listing_location.
            filename: The filename from find_remote_filename.

        Returns:
            The full download URL.
        """
        ...


class NoaaS3Backend(ViirsBackend):
    """Backend for NOAA S3 VIIRS data source."""

    def __init__(self) -> None:
        """Initialise the backend with an empty per-prefix year cache."""
        self._available_years_cache: dict[str, set[int]] = {}

    def get_directory_links(self, base_url: str, location: str, timeout: int) -> list[str]:
        """Return object keys from a public S3 prefix listing."""
        logger.debug("Listing S3 prefix: {}?prefix={}", base_url, location)
        response = requests.get(base_url, params={"prefix": location}, timeout=timeout)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        root = ET.fromstring(response.text)
        entries = [node.text for node in root.findall("s3:Contents/s3:Key", S3_NAMESPACE) if node.text]
        logger.debug("Found {} entries in listing", len(entries))
        return entries

    def find_remote_filename(self, aoi_id: int, entries: list[str]) -> str | None:
        """Locate the matching VIIRS entry for a date/AOI pair."""
        pattern = re.compile(rf"GLB{aoi_id:03d}_.*\.tif$", re.IGNORECASE)
        for entry in entries:
            name = entry.rsplit("/", 1)[-1]
            if pattern.search(name):
                return name
        return None

    def get_listing_location(self, base_url: str, event_date: datetime, data_format: str) -> ListingLocation:
        """Return the S3 prefix and date token for NOAA backend."""
        date_token = event_date.strftime("%Y%m%d")
        prefix = f"{NOAA_VIIRS_PREFIX}/{data_format.upper()}/{event_date.strftime('%Y/%m/%d')}/"
        return ListingLocation(locator=prefix, date_token=date_token)

    def available_years(self, base_url: str, data_format: str, timeout: int) -> set[int] | None:
        """Enumerate published years on the NOAA S3 bucket via a single listing.

        Issues one ``ListObjectsV2``-style request with ``delimiter=/`` against
        ``JPSS_Blended_Products/VFM_1day_GLB/<FORMAT>/`` and parses the
        ``CommonPrefixes`` response into a set of integer years. The result is
        memoised per ``(base_url, data_format)`` for the lifetime of the
        backend instance to avoid repeated round-trips.
        """
        cache_key = f"{base_url}|{data_format.upper()}"
        cached = self._available_years_cache.get(cache_key)
        if cached is not None:
            return cached

        prefix = f"{NOAA_VIIRS_PREFIX}/{data_format.upper()}/"
        try:
            response = requests.get(
                base_url,
                params={"prefix": prefix, "delimiter": "/"},
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.RequestException:
            # Coverage unknown — fall back to per-date probing.
            return None

        root = ET.fromstring(response.text)
        years: set[int] = set()
        for node in root.findall("s3:CommonPrefixes/s3:Prefix", S3_NAMESPACE):
            text = node.text or ""
            tail = text.removeprefix(prefix).rstrip("/")
            if tail.isdigit() and len(tail) == 4:
                years.add(int(tail))

        self._available_years_cache[cache_key] = years
        return years

    def build_result_url(self, base_url: str, listing_location: str, filename: str) -> str:
        """Build the S3 URL for a listing entry."""
        return f"{base_url}/{listing_location}{filename}"


class GmuLegacyBackend(ViirsBackend):
    """Backend for GMU Legacy VIIRS data source."""

    def get_directory_links(self, base_url: str, location: str, timeout: int) -> list[str]:
        """Return entries from a VIIRS directory listing page."""
        response = requests.get(location, timeout=timeout)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return re.findall(r'href=["\']([^"\']+)["\']', response.text)

    def find_remote_filename(self, aoi_id: int, entries: list[str]) -> str | None:
        """Locate the matching VIIRS entry for a date/AOI pair."""
        pattern = re.compile(rf"_005day_{aoi_id:03d}\.tif(?:\.zip)?$", re.IGNORECASE)
        for entry in entries:
            name = entry.rsplit("/", 1)[-1]
            if pattern.search(name):
                return name
        return None

    def get_listing_location(self, base_url: str, event_date: datetime, data_format: str) -> ListingLocation:
        """Return the directory URL and date token for GMU backend."""
        date_token = event_date.strftime("%Y%m%d")
        url = f"{base_url}/{date_token}/tif/"
        return ListingLocation(locator=url, date_token=date_token)

    def build_result_url(self, base_url: str, listing_location: str, filename: str) -> str:
        """Build the download URL for a GMU listing entry."""
        return f"{listing_location}{filename}"


# Backend registry for factory pattern
_BACKEND_REGISTRY: dict[str, type[ViirsBackend]] = {
    "noaa_s3": NoaaS3Backend,
    "gmu_legacy": GmuLegacyBackend,
}


def get_backend(name: str) -> ViirsBackend:
    """Get a backend instance by name.

    Args:
        name: The backend identifier (e.g., "noaa_s3", "gmu_legacy").

    Returns:
        An instance of the requested backend.

    Raises:
        ValueError: If the backend name is not recognized.
    """
    backend_class = _BACKEND_REGISTRY.get(name)
    if backend_class is None:
        supported = ", ".join(sorted(_BACKEND_REGISTRY.keys()))
        raise ValueError(f"Unsupported VIIRS backend '{name}'. Expected one of: {supported}")
    return backend_class()


def list_backends() -> list[str]:
    """Return a list of supported backend names."""
    return list(_BACKEND_REGISTRY.keys())
