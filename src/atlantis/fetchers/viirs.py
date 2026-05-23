"""VIIRS flood detection fetcher via web scraping.

VIIRS provides flood detection from Suomi-NPP and NOAA-20 satellites.
"""

import re
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zipfile import ZipFile

import geopandas as gpd
import numpy as np
import rasterio
import requests
from bs4 import BeautifulSoup
from rasterio.io import MemoryFile
from rasterio.mask import mask
from rasterio.merge import merge
from shapely.geometry import box

from atlantis.config import get_config
from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult, SearchResult
from atlantis.fetchers.registry import register_fetcher
from atlantis.models.event import FloodEvent
from atlantis.models.metadata import TileMetadata
from atlantis.utils.io import download_file, ensure_dir

if TYPE_CHECKING:
    import xarray as xr


FLOOD_MIN_CODE = 160
CLOUD_CODES = {30}
PERMANENT_WATER_CODES = {17}
SEASONAL_WATER_CODES = {20}
OPEN_WATER_CODES = {99}


def _date_range(start: datetime, end: datetime) -> list[datetime]:
    dates: list[datetime] = []
    current = start
    while current <= end:
        dates.append(current)
        current += timedelta(days=1)
    return dates


@register_fetcher("viirs")
class VIIRSFetcher(AbstractFloodFetcher):
    """Fetcher for VIIRS flood detection data via web scraping.

    VIIRS flood products are derived from the Day-Night Band (DNB)
    and provide inundation detection at 375m resolution.
    """

    source_id: str = "viirs"

    def __init__(self, base_url: str | None = None, timeout: int | None = None) -> None:
        """Initialize the VIIRS fetcher.

        Args:
            base_url: Optional base URL for VIIRS data. Defaults to JPSS Flood archive.
            timeout: Request timeout in seconds.
        """
        config = get_config()
        self.base_url = (base_url or config.fetcher.viirs_base_url or "https://jpssflood.gmu.edu/downloads/pub").rstrip(
            "/"
        )
        self.timeout = timeout or config.fetcher.timeout

    @property
    def aoi_path(self) -> Path:
        """Return the packaged AOI grid path."""
        return Path(__file__).with_name("data") / "viirs_aois.geojson"

    def _load_aois(self) -> gpd.GeoDataFrame:
        """Load the packaged VIIRS AOI grid."""
        if not self.aoi_path.exists():
            raise FileNotFoundError(f"VIIRS AOI grid not found at {self.aoi_path}")
        return gpd.read_file(self.aoi_path).to_crs("EPSG:4326")

    def _event_dates(self, event: FloodEvent) -> list[datetime]:
        """Expand the event date range into daily datetimes."""
        start = datetime.combine(event.start_date, time.min, tzinfo=timezone.utc)
        end = datetime.combine(event.end_date, time.min, tzinfo=timezone.utc)
        return _date_range(start, end)

    def _intersecting_aois(self, event: FloodEvent) -> gpd.GeoDataFrame:
        """Find packaged AOIs intersecting the event bbox."""
        area_geom = box(*event.bbox)
        aois = self._load_aois()
        hits = aois[aois.intersects(area_geom)].copy()
        return hits.sort_values("AOI_ID").reset_index(drop=True)

    def _get_directory_links(self, url: str) -> list[str]:
        """Return href entries from a VIIRS directory listing page."""
        response = requests.get(url, timeout=self.timeout)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        return [href for href in (link.get("href", "") for link in soup.find_all("a")) if href]

    def _find_remote_filename(self, aoi_id: int, hrefs: list[str]) -> str | None:
        """Locate the matching VIIRS ZIP entry for a date/AOI pair."""
        pattern = re.compile(rf"_005day_{aoi_id:03d}\.tif(?:\.zip)?$")
        for href in hrefs:
            name = href.rsplit("/", 1)[-1]
            if pattern.search(name):
                return name
        return None

    def _extract_tif(self, zip_path: Path, output_dir: Path) -> Path:
        """Extract the first TIFF payload from a VIIRS ZIP file."""
        with ZipFile(zip_path) as archive:
            tif_members = [member for member in archive.namelist() if member.lower().endswith(".tif")]
            if not tif_members:
                raise FileNotFoundError(f"No TIFF file found inside {zip_path}")
            member = tif_members[0]
            target = output_dir / Path(member).name
            if target.exists():
                return target
            archive.extract(member, path=output_dir)
            extracted = output_dir / member
            if extracted != target:
                extracted.replace(target)
                extracted.parent.rmdir()
            return target

    def _build_metadata(
        self,
        event: FloodEvent,
        clipped_transform: rasterio.Affine,
        width: int,
        height: int,
        cloud_fraction: float,
    ) -> TileMetadata:
        """Build TileMetadata for a clipped VIIRS composite."""
        west = clipped_transform.c
        east = clipped_transform.c + clipped_transform.a * width
        north = clipped_transform.f
        south = clipped_transform.f + clipped_transform.e * height
        return TileMetadata(
            event_id=event.event_id,
            source_id=self.source_id,
            fetch_timestamp=datetime.now(timezone.utc),
            crs="EPSG:4326",
            resolution=abs(clipped_transform.a),
            bbox=(min(west, east), min(south, north), max(west, east), max(south, north)),
            cloud_fraction=cloud_fraction,
            quality_bitmask=0,
            permanent_water_mask_available=True,
        )

    def search(self, event: FloodEvent) -> list[SearchResult]:
        """Search for VIIRS data for the given flood event.

        Args:
            event: The flood event to search for.

        Returns:
            List of search results.
        """
        aois = self._intersecting_aois(event)
        if aois.empty:
            return []

        results: list[SearchResult] = []
        for event_date in self._event_dates(event):
            date_token = event_date.strftime("%Y%m%d")
            listing_url = f"{self.base_url}/{date_token}/tif/"
            hrefs = self._get_directory_links(listing_url)
            if not hrefs:
                continue

            for row in aois.itertuples():
                filename = self._find_remote_filename(int(row.AOI_ID), hrefs)
                if filename is None:
                    continue
                bounds = row.geometry.bounds
                results.append(
                    SearchResult(
                        source_id=self.source_id,
                        item_id=f"viirs:{date_token}:{int(row.AOI_ID):03d}",
                        timestamp=event_date,
                        bbox=(bounds[0], bounds[1], bounds[2], bounds[3]),
                        url=f"{listing_url}{filename}",
                        properties={"aoi_id": int(row.AOI_ID), "date": date_token, "filename": filename},
                    )
                )

        return results

    def fetch(self, event: FloodEvent, output_dir: Path) -> list[FetchResult]:
        """Fetch VIIRS data for the given flood event.

        Args:
            event: The flood event to fetch data for.
            output_dir: Directory to save downloaded files.

        Returns:
            List of fetch results.
        """
        search_results = self.search(event)
        if not search_results:
            return []

        output_dir = ensure_dir(output_dir)
        raw_dir = ensure_dir(output_dir / "raw")
        processed_dir = ensure_dir(output_dir / "processed")
        grouped_results: dict[str, list[SearchResult]] = defaultdict(list)
        for result in search_results:
            grouped_results[result.properties["date"]].append(result)

        fetch_results: list[FetchResult] = []
        area_geom = box(*event.bbox)

        for date_token, dated_results in sorted(grouped_results.items()):
            tile_paths: list[Path] = []
            for result in sorted(dated_results, key=lambda item: item.properties["aoi_id"]):
                filename = result.properties["filename"]
                zip_name = filename if filename.endswith(".zip") else f"{filename}.zip"
                zip_path = raw_dir / zip_name
                download_file(result.url, output_path=zip_path)
                tile_paths.append(self._extract_tif(zip_path, raw_dir))

            if not tile_paths:
                continue

            srcs = [rasterio.open(tile_path) for tile_path in tile_paths]
            try:
                mosaic, transform = merge(srcs)
                meta = srcs[0].meta.copy()
            finally:
                for src in srcs:
                    src.close()

            meta.update(
                {
                    "height": mosaic.shape[1],
                    "width": mosaic.shape[2],
                    "transform": transform,
                }
            )

            with MemoryFile() as memory_file:
                with memory_file.open(**meta) as dataset:
                    dataset.write(mosaic)
                    clipped, clipped_transform = mask(dataset, [area_geom], crop=True)

            data = clipped[0]
            cloud = np.isin(data, list(CLOUD_CODES))
            permanent_water = np.isin(data, list(PERMANENT_WATER_CODES))
            seasonal_water = np.isin(data, list(SEASONAL_WATER_CODES))
            open_water = np.isin(data, list(OPEN_WATER_CODES))
            flood = data >= FLOOD_MIN_CODE

            flood_extent = np.zeros_like(data, dtype=np.uint8)
            flood_extent[flood] = 1

            quality_mask = np.ones_like(data, dtype=np.uint8)
            quality_mask[cloud | permanent_water | seasonal_water | open_water] = 0

            permanent_water_mask = np.zeros_like(data, dtype=np.uint8)
            permanent_water_mask[permanent_water] = 1

            base_name = f"{event.event_id}_{date_token}_viirs"
            obs_path = processed_dir / f"{base_name}_flood_extent.tif"
            quality_path = processed_dir / f"{base_name}_quality_mask.tif"
            perm_water_path = processed_dir / f"{base_name}_permanent_water.tif"

            output_meta = {
                "driver": "GTiff",
                "height": flood_extent.shape[0],
                "width": flood_extent.shape[1],
                "count": 1,
                "dtype": "uint8",
                "crs": "EPSG:4326",
                "transform": clipped_transform,
                "nodata": 0,
                "compress": "LZW",
            }

            for path, array in (
                (obs_path, flood_extent),
                (quality_path, quality_mask),
                (perm_water_path, permanent_water_mask),
            ):
                with rasterio.open(path, "w", **output_meta) as dst:
                    dst.write(array, 1)

            metadata = self._build_metadata(
                event,
                clipped_transform,
                flood_extent.shape[1],
                flood_extent.shape[0],
                float(cloud.sum() / cloud.size) if cloud.size else 0.0,
            )
            fetch_results.append(
                FetchResult(
                    event_id=event.event_id,
                    source_id=self.source_id,
                    files=[obs_path, quality_path, perm_water_path],
                    metadata=metadata,
                )
            )

        return fetch_results

    def to_dataset(self, result: FetchResult) -> "xr.Dataset":  # type: ignore[name-defined]
        """Convert VIIRS fetch result to xarray Dataset.

        Args:
            result: The fetch result to convert.

        Returns:
            xarray Dataset with VIIRS data.
        """
        try:
            import rioxarray as rxr
            import xarray as xr
        except ImportError as exc:  # pragma: no cover - exercised by environment setup
            raise ImportError("rioxarray and xarray are required to read VIIRS datasets") from exc

        files_by_name = {path.name: path for path in result.files}
        obs_path = next(path for name, path in files_by_name.items() if name.endswith("_flood_extent.tif"))
        quality_path = next(path for name, path in files_by_name.items() if name.endswith("_quality_mask.tif"))
        permanent_water_path = next(
            path for name, path in files_by_name.items() if name.endswith("_permanent_water.tif")
        )

        flood_extent = rxr.open_rasterio(obs_path).squeeze(drop=True).astype("float32").rename("flood_extent")
        quality_mask = rxr.open_rasterio(quality_path).squeeze(drop=True).astype("uint8").rename("quality_mask")
        permanent_water = (
            rxr.open_rasterio(permanent_water_path).squeeze(drop=True).astype("uint8").rename("permanent_water")
        )

        dataset = xr.Dataset(
            {
                "flood_extent": flood_extent,
                "quality_mask": quality_mask,
                "permanent_water": permanent_water,
            }
        )
        dataset.attrs["source_id"] = self.source_id
        dataset.attrs["event_id"] = result.event_id
        return dataset
