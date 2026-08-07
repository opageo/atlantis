"""STAC backend for GFM data discovery on EODC."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from time import perf_counter
from typing import TYPE_CHECKING

from loguru import logger
from shapely.geometry import box

if TYPE_CHECKING:
    from pystac import ItemCollection

from atlantis.models.event import FloodEvent

#: Default EODC STAC API endpoint.
DEFAULT_GFM_STAC_URL = "https://stac.eodc.eu/api/v1"

#: STAC collection ID for GFM data.
GFM_COLLECTION_ID = "GFM"


class GfmStacBackend:
    """Backend for searching GFM flood products via the EODC STAC API.

    Attributes:
        api_url: STAC API endpoint URL.
        collection_id: STAC collection identifier.
        max_items: Maximum items to return per search.
    """

    def __init__(
        self,
        api_url: str = DEFAULT_GFM_STAC_URL,
        collection_id: str = GFM_COLLECTION_ID,
        max_items: int = 1000,
        request_timeout: float = 30.0,
        max_retries: int = 1,
    ) -> None:
        """Initialise the backend with STAC endpoint, collection, and result cap."""
        self.api_url = api_url
        self.collection_id = collection_id
        self.max_items = max_items
        self.request_timeout = request_timeout
        self.max_retries = max_retries

    def search(self, event: FloodEvent) -> "ItemCollection":
        """Search the EODC STAC for GFM items matching the flood event.

        Args:
            event: Flood event with bbox and date range.

        Returns:
            pystac ItemCollection of matching items.
        """
        from pystac_client import Client
        from pystac_client.stac_api_io import StacApiIO

        west, south, east, north = event.bbox
        aoi = box(west, south, east, north)

        start = datetime(event.start_date.year, event.start_date.month, event.start_date.day, 0, 0, 0)
        end = datetime(event.end_date.year, event.end_date.month, event.end_date.day, 23, 59, 59)

        logger.info(
            "Searching GFM STAC: bbox={}, period={} to {}",
            event.bbox,
            start.isoformat(),
            end.isoformat(),
        )

        started = perf_counter()
        logger.info(
            "Connecting to GFM STAC API (timeout={}s, max_retries={})",
            self.request_timeout,
            self.max_retries,
        )
        try:
            catalog = Client.open(
                self.api_url,
                timeout=self.request_timeout,
                stac_io=StacApiIO(timeout=self.request_timeout, max_retries=self.max_retries),
            )
        except Exception as exc:
            logger.error(
                "GFM STAC connection failed after {:.1f}s: {}",
                perf_counter() - started,
                exc,
            )
            raise
        logger.info("Connected to GFM STAC API in {:.1f}s", perf_counter() - started)
        search = catalog.search(
            max_items=self.max_items,
            collections=self.collection_id,
            intersects=aoi,
            datetime=(start, end),
        )

        logger.info("Executing GFM STAC search")
        items = search.item_collection()
        logger.info("Found {} GFM items in {:.1f}s", len(items), perf_counter() - started)
        return items

    @staticmethod
    def group_items_by_date(items: "ItemCollection") -> dict[str, list]:
        """Group STAC items by acquisition date.

        Args:
            items: STAC ItemCollection to group.

        Returns:
            Dictionary mapping date strings (YYYYMMDD) to lists of items.
        """
        groups: dict[str, list] = defaultdict(list)
        for item in items:
            dt = item.datetime
            if dt is None:
                # Try to extract from item properties
                dt_str = item.properties.get("datetime", "")
                if dt_str:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                else:
                    logger.warning("Item {} has no datetime, skipping", item.id)
                    continue
            date_key = dt.strftime("%Y%m%d")
            groups[date_key].append(item)

        return dict(groups)
