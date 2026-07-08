"""Backend abstractions for MODIS MCDWD data sources.

Implements the Strategy pattern (mirroring
:mod:`atlantis.fetchers.viirs.backend`) for the two MCDWD distribution paths:

- :class:`LanceGeotiffBackend` — LANCE NRT single-composite GeoTIFFs
  (``MCDWD_L3_F{1,1C,2,3}_NRT``). Discovered via the LANCE JSON listing API
  and consumed by ``/vsicurl/`` streaming.
- :class:`LaadsHdf4Backend` — LAADS reprocessed (``MCDWD_L3``) and archived
  NRT (``MCDWD_L3_NRT``) HDF4 files. Discovered via HTML directory scraping
  and consumed by download-and-extract.

Both servers require an Earthdata Login bearer token; see
:func:`get_earthdata_token`.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar

import requests
from loguru import logger

LANCE_NRT_PRODUCT_PATTERN = "MCDWD_L3_{composite}_NRT"
LANCE_NRT_HDF_PRODUCT = "MCDWD_L3_NRT"
LAADS_REPROCESSED_SHORTNAME = "MCDWD_L3"
LAADS_NRT_ARCHIVE_SHORTNAME = "MCDWD_L3_NRT"

# Cutover year between the LAADS reprocessed (MCDWD_L3, ≤2025) and the
# archived NRT (MCDWD_L3_NRT, ≥2026) collections.
LAADS_REPROCESSED_LAST_YEAR = 2025

# 13-digit production timestamp YYYYDDDHHMMSS embedded in NRT/reprocessed filenames.
PRODTIMESTAMP_PATTERN = re.compile(r"\.(\d{13})\.")

# LANCE rolling NRT window (~1 week of files retained).
LANCE_RETENTION_DAYS = 14


class MissingEarthdataTokenError(RuntimeError):
    """Raised when the EARTHDATA_TOKEN environment variable is not set."""


def get_earthdata_token() -> str:
    """Return the Earthdata bearer token from the environment.

    Raises:
        MissingEarthdataTokenError: if ``EARTHDATA_TOKEN`` is unset or empty.
    """
    token = os.environ.get("EARTHDATA_TOKEN", "").strip()
    if not token:
        raise MissingEarthdataTokenError(
            "Missing EARTHDATA_TOKEN. Register at https://urs.earthdata.nasa.gov/, "
            "then run: export EARTHDATA_TOKEN='YOUR_TOKEN'"
        )
    return token


def earthdata_auth_headers() -> dict[str, str]:
    """Build the HTTP headers required by NASA LAADS / LANCE."""
    return {"Authorization": f"Bearer {get_earthdata_token()}"}


@dataclass(frozen=True)
class ListingLocation:
    """Location information for directory/listing queries.

    Attributes:
        locator: The base path or URL fragment for the listing query.
        date_token: The date string token used in item identification (``YYYYMMDD``).
    """

    locator: str
    date_token: str


@dataclass(frozen=True)
class ModisListingEntry:
    """A single entry returned by a backend listing.

    Attributes:
        filename: The bare filename (no directory).
        url: Optional fully-qualified download URL when the listing supplies one.
        prod_timestamp: Optional 13-digit ``YYYYDDDHHMMSS`` production
            timestamp parsed from the filename. Used to detect in-place NRT
            updates; absent on legacy LAADS reprocessed files.
    """

    filename: str
    url: str | None = None
    prod_timestamp: str | None = None


def parse_prod_timestamp(filename: str) -> str | None:
    """Extract the 13-digit production timestamp from a MODIS filename, if present."""
    match = PRODTIMESTAMP_PATTERN.search(filename)
    return match.group(1) if match else None


class ModisBackend(ABC):
    """Abstract base class for MODIS MCDWD data source backends.

    Each backend encapsulates:
    - directory/listing discovery (HTML scrape vs JSON API)
    - filename matching for a given ``(date, h, v, composite)`` triple
    - URL construction for download or streaming
    - whether the backend supports streaming via ``/vsicurl/``

    Subclasses must populate :attr:`supports_streaming` and implement the
    five abstract methods.
    """

    #: Stable identifier (e.g. ``"lance_geotiff"``).
    name: ClassVar[str] = ""

    #: True when the backend exposes flat single-band GeoTIFFs that GDAL
    #: can range-read via ``/vsicurl/``. False for HDF4 archives.
    supports_streaming: ClassVar[bool] = False

    @abstractmethod
    def get_listing_location(self, base_url: str, event_date: datetime, composite: str) -> ListingLocation:
        """Return the listing location for ``(date, composite)``.

        Args:
            base_url: The base URL for the backend.
            event_date: Calendar date to query.
            composite: One of ``"F1"``, ``"F1C"``, ``"F2"``, ``"F3"`` (used by
                the LANCE GeoTIFF backend; ignored by the LAADS HDF4 backend
                because all four composites live in one HDF4 file).

        Returns:
            A :class:`ListingLocation` carrying the listing path and date token.
        """
        ...

    @abstractmethod
    def get_directory_listing(
        self,
        base_url: str,
        location: ListingLocation,
        timeout: int,
        *,
        headers: dict[str, str] | None = None,
    ) -> list[ModisListingEntry]:
        """Return the raw entries for a listing.

        Args:
            base_url: The base URL for the backend.
            location: Output of :meth:`get_listing_location`.
            timeout: Request timeout in seconds.
            headers: Optional HTTP headers (defaults to Earthdata bearer when ``None``).

        Returns:
            List of :class:`ModisListingEntry` records. May be empty when the
            listing is reachable but contains no files (e.g. the date is
            outside the LANCE retention window).
        """
        ...

    @abstractmethod
    def find_remote_filename(
        self,
        h: int,
        v: int,
        composite: str,
        entries: list[ModisListingEntry],
    ) -> ModisListingEntry | None:
        """Locate the entry matching a given ``(h, v, composite)`` triple."""
        ...

    @abstractmethod
    def build_result_url(self, base_url: str, location: ListingLocation, entry: ModisListingEntry) -> str:
        """Build the downloadable URL for a listing entry."""
        ...

    def available_years(self, base_url: str, timeout: int) -> set[int] | None:
        """Return the set of calendar years the backend publishes.

        Default returns ``None`` (unknown — fall back to per-date probing).
        Subclasses with deterministic coverage may override.
        """
        return None


# ── LANCE GeoTIFF backend (streamable, JSON-driven) ──────────────────────


class LanceGeotiffBackend(ModisBackend):
    """LANCE single-composite GeoTIFF backend.

    Discovers files via the LANCE JSON listing API
    (``/api/v2/content/details``) and serves URLs that GDAL can range-read
    via ``/vsicurl/``. Coverage is the rolling ~1-week NRT window.

    The LANCE service has two independent mirrors (``nrt3`` primary,
    ``nrt4`` backup) — when initialised with both, this backend will fall
    back to the backup automatically on connection errors.
    """

    name: ClassVar[str] = "lance_geotiff"
    supports_streaming: ClassVar[bool] = True

    def __init__(self, backup_base_url: str | None = None) -> None:
        """Initialise the backend.

        Args:
            backup_base_url: Optional ``nrt4`` mirror URL used as fallback
                when the primary returns a connection-level error.
        """
        self.backup_base_url = backup_base_url.rstrip("/") if backup_base_url else None

    # The JSON listing API needs the year and 3-digit DOY:
    #   /api/v2/content/details?products=<PRODUCT>&archiveSets=61&temporalRanges=YYYY-DDD
    JSON_LISTING_PATH = "/api/v2/content/details"

    def _product_for(self, composite: str) -> str:
        return LANCE_NRT_PRODUCT_PATTERN.format(composite=composite.upper())

    def get_listing_location(self, base_url: str, event_date: datetime, composite: str) -> ListingLocation:
        """Build the LANCE archive locator for ``(date, composite)``."""
        # Encode year + DOY directly into the locator so it can be reused.
        year = event_date.strftime("%Y")
        doy = event_date.strftime("%j")
        product = self._product_for(composite)
        # Locator is the directory prefix used by build_result_url(); the
        # JSON discovery uses the same year/doy via _temporal_range().
        locator = f"archive/allData/61/{product}/{year}/{doy}/"
        return ListingLocation(
            locator=locator,
            date_token=event_date.strftime("%Y%m%d"),
        )

    @staticmethod
    def _temporal_range(date_token: str) -> str:
        """Return ``YYYY-DDD`` from a ``YYYYMMDD`` token (LANCE JSON API format)."""
        dt = datetime.strptime(date_token, "%Y%m%d")
        return f"{dt.year}-{dt.strftime('%j')}"

    @staticmethod
    def _product_from_locator(locator: str) -> str:
        # archive/allData/61/<PRODUCT>/<YEAR>/<DOY>/
        parts = locator.strip("/").split("/")
        return parts[3]

    def get_directory_listing(
        self,
        base_url: str,
        location: ListingLocation,
        timeout: int,
        *,
        headers: dict[str, str] | None = None,
    ) -> list[ModisListingEntry]:
        """Hit the LANCE JSON listing API; fall back to the ``nrt4`` mirror."""
        headers = headers if headers is not None else earthdata_auth_headers()
        product = self._product_from_locator(location.locator)
        params = {
            "products": product,
            "archiveSets": "61",
            "temporalRanges": self._temporal_range(location.date_token),
        }

        bases_to_try = [base_url]
        if self.backup_base_url and self.backup_base_url != base_url:
            bases_to_try.append(self.backup_base_url)

        last_error: requests.RequestException | None = None
        for candidate in bases_to_try:
            url = candidate.rstrip("/") + self.JSON_LISTING_PATH
            try:
                logger.debug("LANCE JSON listing: GET {} params={}", url, params)
                response = requests.get(url, params=params, headers=headers, timeout=timeout)
                if response.status_code == 404:
                    return []
                response.raise_for_status()
                return self._parse_json_listing(response.text, base_url=candidate)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning(
                    "LANCE listing failed at {} ({}); trying mirror if available",
                    candidate,
                    exc,
                )
                continue

        if last_error is not None:
            raise last_error
        return []

    @staticmethod
    def _parse_json_listing(payload: str, *, base_url: str) -> list[ModisListingEntry]:
        """Parse the LANCE ``content/details`` response into entries.

        The endpoint returns a JSON object whose ``content`` field is a list
        of items; each item carries a ``name`` (filename) and a ``downloadsLink``
        (full URL). We accept both shapes (top-level list and ``{"content": [...]}``)
        defensively because the API has varied across releases.
        """
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("LANCE JSON listing: malformed JSON response")
            return []

        if isinstance(data, dict):
            items = data.get("content", []) or data.get("items", [])
        elif isinstance(data, list):
            items = data
        else:
            return []

        entries: list[ModisListingEntry] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            filename = item.get("name") or item.get("fileName")
            if not filename:
                continue
            # downloadsLink is a relative path on some responses, full URL on others.
            link = item.get("downloadsLink") or item.get("downloadLink")
            url: str | None = None
            if link:
                url = link if link.startswith("http") else base_url.rstrip("/") + "/" + link.lstrip("/")
            entries.append(
                ModisListingEntry(
                    filename=filename,
                    url=url,
                    prod_timestamp=parse_prod_timestamp(filename),
                )
            )
        logger.debug("LANCE JSON listing: {} entries", len(entries))
        return entries

    def find_remote_filename(
        self,
        h: int,
        v: int,
        composite: str,
        entries: list[ModisListingEntry],
    ) -> ModisListingEntry | None:
        """Match a single ``(h, v, composite)`` tile from a LANCE listing."""
        product = self._product_for(composite)
        tile_token = f"h{h:02d}v{v:02d}"
        # Filename schema: <PRODUCT>.A<YYYYDDD>.h<XX>v<YY>.061.<TS>.tif
        pattern = re.compile(
            rf"^{re.escape(product)}\.A\d{{7}}\.{tile_token}\.061(\.\d{{13}})?\.tif$",
            re.IGNORECASE,
        )
        for entry in entries:
            if pattern.match(entry.filename):
                return entry
        return None

    def build_result_url(self, base_url: str, location: ListingLocation, entry: ModisListingEntry) -> str:
        """Return the entry's URL, synthesising one from base + locator if absent."""
        if entry.url:
            return entry.url
        return f"{base_url.rstrip('/')}/{location.locator}{entry.filename}"


# ── LAADS HDF4 backend (download-only, HTML-driven) ──────────────────────


@dataclass(frozen=True)
class _LaadsShortname:
    """LAADS shortname split for a given year."""

    shortname: str
    layout_dir: str  # the same as shortname; kept for forward extensibility

    @classmethod
    def for_year(cls, year: int) -> "_LaadsShortname":
        if year <= LAADS_REPROCESSED_LAST_YEAR:
            return cls(LAADS_REPROCESSED_SHORTNAME, LAADS_REPROCESSED_SHORTNAME)
        return cls(LAADS_NRT_ARCHIVE_SHORTNAME, LAADS_NRT_ARCHIVE_SHORTNAME)


class LaadsHdf4Backend(ModisBackend):
    """LAADS DAAC HDF4 backend (download only).

    Discovers files via HTML directory listings and downloads each ``.hdf``
    in full before handing it to the processor. Streaming via ``/vsicurl/``
    is not supported because HDF4 lacks an internal chunked layout
    suitable for HTTP range reads.

    Performs a fail-fast check at construction that GDAL was built with
    HDF4 support (the ``HDF4`` driver must be registered).
    """

    name: ClassVar[str] = "laads_hdf4"
    supports_streaming: ClassVar[bool] = False

    def __init__(self) -> None:
        """Verify GDAL was built with HDF4 support; fail fast otherwise."""
        self._verify_hdf4_driver()

    @staticmethod
    def _verify_hdf4_driver() -> None:
        try:
            from osgeo import gdal  # type: ignore[import-not-found]
        except ImportError:
            # rasterio bundles GDAL but does not expose it via osgeo when
            # built from the PyPI wheel; fall back to rasterio.drivers.
            try:
                import rasterio

                drivers = set(rasterio.drivers.raster_driver_extensions().values())
                if "HDF4" not in drivers and "HDF4Image" not in drivers:
                    raise RuntimeError("HDF4 driver not registered")
            except Exception as exc:  # pragma: no cover - safety net
                raise RuntimeError("GDAL with HDF4 support is required for the laads_hdf4 backend. ") from exc
            return

        driver_names = {gdal.GetDriver(i).ShortName for i in range(gdal.GetDriverCount())}
        if "HDF4" not in driver_names and "HDF4Image" not in driver_names:
            raise RuntimeError("GDAL with HDF4 support is required for the laads_hdf4 backend. ")

    def get_listing_location(self, base_url: str, event_date: datetime, composite: str) -> ListingLocation:
        """Pick the LAADS shortname (``MCDWD_L3`` vs ``MCDWD_L3_NRT``) and build the locator."""
        shortname = _LaadsShortname.for_year(event_date.year).shortname
        year = event_date.strftime("%Y")
        doy = event_date.strftime("%j")
        locator = f"archive/allData/61/{shortname}/{year}/{doy}/"
        return ListingLocation(
            locator=locator,
            date_token=event_date.strftime("%Y%m%d"),
        )

    def get_directory_listing(
        self,
        base_url: str,
        location: ListingLocation,
        timeout: int,
        *,
        headers: dict[str, str] | None = None,
    ) -> list[ModisListingEntry]:
        """Scrape the LAADS HTML directory listing for ``.hdf`` entries."""
        headers = headers if headers is not None else earthdata_auth_headers()
        url = f"{base_url.rstrip('/')}/{location.locator}"
        logger.debug("LAADS HTML listing: GET {}", url)
        response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        # Match href values that look like an MCDWD HDF filename.
        matches = re.findall(r'href="([^"]*MCDWD_L3[^"]*\.hdf)"', response.text, flags=re.IGNORECASE)
        seen: set[str] = set()
        entries: list[ModisListingEntry] = []
        for href in matches:
            filename = href.rsplit("/", 1)[-1]
            if filename in seen:
                continue
            seen.add(filename)
            entries.append(
                ModisListingEntry(
                    filename=filename,
                    url=None,
                    prod_timestamp=parse_prod_timestamp(filename),
                )
            )
        logger.debug("LAADS HTML listing: {} entries", len(entries))
        return entries

    def find_remote_filename(
        self,
        h: int,
        v: int,
        composite: str,
        entries: list[ModisListingEntry],
    ) -> ModisListingEntry | None:
        """Match a single ``(h, v)`` tile (composite is irrelevant for HDF4)."""
        # composite is irrelevant — all four flood layers ship in one HDF4.
        tile_token = f"h{h:02d}v{v:02d}"
        # Filename schema: <SHORTNAME>.A<YYYYDDD>.h<XX>v<YY>.061[.<TS>].hdf
        pattern = re.compile(
            rf"^MCDWD_L3(?:_NRT)?\.A\d{{7}}\.{tile_token}\.061(\.\d{{13}})?\.hdf$",
            re.IGNORECASE,
        )
        for entry in entries:
            if pattern.match(entry.filename):
                return entry
        return None

    def build_result_url(self, base_url: str, location: ListingLocation, entry: ModisListingEntry) -> str:
        """Concatenate base URL + locator + filename for a LAADS download."""
        return f"{base_url.rstrip('/')}/{location.locator}{entry.filename}"

    def available_years(self, base_url: str, timeout: int) -> set[int] | None:
        """Hard-code the published LAADS year range (2003+, see modis.md)."""
        # MCDWD_L3 reprocessed = 2003–2025. NRT archive on LAADS = 2026 onward.
        # We cap at the current calendar year to avoid claiming future coverage.
        current_year = datetime.now(timezone.utc).year
        years = set(range(2003, LAADS_REPROCESSED_LAST_YEAR + 1))
        years.update(range(LAADS_REPROCESSED_LAST_YEAR + 1, max(current_year + 1, LAADS_REPROCESSED_LAST_YEAR + 1)))
        return years


# ── Registry helpers ─────────────────────────────────────────────────────


_BACKEND_REGISTRY: dict[str, type[ModisBackend]] = {
    LanceGeotiffBackend.name: LanceGeotiffBackend,
    LaadsHdf4Backend.name: LaadsHdf4Backend,
}


def get_backend(name: str, **kwargs) -> ModisBackend:
    """Instantiate a MODIS backend by name."""
    backend_cls = _BACKEND_REGISTRY.get(name)
    if backend_cls is None:
        supported = ", ".join(sorted(_BACKEND_REGISTRY.keys()))
        raise ValueError(f"Unsupported MODIS backend '{name}'. Expected one of: {supported}")
    return backend_cls(**kwargs)


def list_backends() -> list[str]:
    """Return a list of supported MODIS backend names."""
    return list(_BACKEND_REGISTRY.keys())
