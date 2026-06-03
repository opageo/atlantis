"""VIIRS flood detection fetcher.

VIIRS provides flood detection from Suomi-NPP and NOAA-20 satellites.
"""

from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zipfile import ZipFile

import geopandas as gpd
from shapely.geometry import box

from atlantis.config import get_config
from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult, SearchResult
from atlantis.fetchers.registry import register_fetcher
from atlantis.fetchers.viirs.backend import get_backend, list_backends
from atlantis.fetchers.viirs.dataset import processed_tile_to_dataset
from atlantis.fetchers.viirs.processor import OutputPaths, ProcessTilesResult, ViirsRasterProcessor
from atlantis.fetchers.viirs.selection import flood_pixel_count
from atlantis.fetchers.viirs.selection import is_better_peak_candidate as is_better_peak_candidate
from atlantis.models.event import FloodEvent
from atlantis.utils.io import download_file, ensure_dir

if TYPE_CHECKING:
    import xarray as xr


DEFAULT_NOAA_VIIRS_BASE_URL = "https://noaa-jpss.s3.amazonaws.com"
DEFAULT_GMU_VIIRS_BASE_URL = "https://jpssflood.gmu.edu/downloads/pub"
VIIRS_SUPPORTED_FORMATS = {"tif", "netcdf", "shapezip", "png"}
VIIRS_IMPLEMENTED_FORMATS = {"tif"}


def _date_range(start: datetime, end: datetime) -> list[datetime]:
    """Generate a list of daily datetimes from start to end (inclusive)."""
    dates: list[datetime] = []
    current = start
    while current <= end:
        dates.append(current)
        current += __import__("datetime").timedelta(days=1)
    return dates


def _normalise_backend(value: str) -> str:
    """Normalise and validate backend name."""
    backend = value.strip().lower()
    supported = set(list_backends())
    if backend not in supported:
        supported_str = ", ".join(sorted(supported))
        raise ValueError(f"Unsupported VIIRS backend '{value}'. Expected one of: {supported_str}")
    return backend


def _normalise_format(value: str) -> str:
    """Normalise and validate format name."""
    aliases = {
        "nc": "netcdf",
        "netcdf": "netcdf",
        "shapefile": "shapezip",
        "shape": "shapezip",
        "shapezip": "shapezip",
        "tif": "tif",
        "tiff": "tif",
        "png": "png",
    }
    normalised = aliases.get(value.strip().lower())
    if normalised is None or normalised not in VIIRS_SUPPORTED_FORMATS:
        supported = ", ".join(sorted(VIIRS_SUPPORTED_FORMATS))
        raise ValueError(f"Unsupported VIIRS format '{value}'. Expected one of: {supported}")
    if normalised not in VIIRS_IMPLEMENTED_FORMATS:
        raise NotImplementedError(
            f"VIIRS format '{normalised}' is not implemented yet. Only 'tif' is currently supported."
        )
    return normalised


@register_fetcher("viirs")
class VIIRSFetcher(AbstractFloodFetcher):
    """Fetcher for VIIRS flood detection data.

    VIIRS flood products are derived from the Day-Night Band (DNB)
    and provide inundation detection at 375m resolution.
    """

    source_id: str = "viirs"

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int | None = None,
        backend: str | None = None,
        data_format: str | None = None,
        classify: bool = False,
        stream: bool = False,
        strategy: str = "peak",
        keep_processed: bool = True,
    ) -> None:
        """Initialize the VIIRS fetcher.

        Args:
            base_url: Optional base URL for VIIRS data.
            timeout: Request timeout in seconds.
            backend: VIIRS backend selection.
            data_format: VIIRS data format selection.
            classify: Whether to classify pixels into discrete layers.
            stream: If True, stream remote tiles via GDAL ``/vsicurl/``
                instead of downloading them to disk. Saves local storage
                at the cost of network dependency during processing.
            strategy: How to handle multiple dates: "peak" (best flood date),
                "aggregate" (mean/mode), or "all" (every date).
            keep_processed: When True, write intermediate processed/ GeoTIFFs.
        """
        config = get_config()

        self.backend_name = _normalise_backend(backend or config.fetcher.viirs_backend)
        self.backend = get_backend(self.backend_name)
        self.data_format = _normalise_format(data_format or config.fetcher.viirs_format)
        self.base_url = self._resolve_base_url(base_url, config)
        self.timeout = timeout or config.fetcher.timeout
        self.classify = classify
        self.stream = stream
        self.strategy = strategy
        self.keep_processed = keep_processed

        if self.strategy not in {"peak", "aggregate", "all"}:
            raise ValueError(f"Invalid strategy '{self.strategy}'. Expected 'peak', 'aggregate', or 'all'.")

    def _resolve_base_url(self, override: str | None, config) -> str:
        """Determine the base URL based on backend and configuration."""
        if override is not None:
            return override.rstrip("/")

        if self.backend_name == "noaa_s3":
            url = config.fetcher.viirs_base_url or DEFAULT_NOAA_VIIRS_BASE_URL
        else:
            url = config.fetcher.viirs_legacy_base_url or DEFAULT_GMU_VIIRS_BASE_URL

        return url.rstrip("/")

    @property
    def aoi_path(self) -> Path:
        """Return the packaged AOI grid path.

        The GeoJSON lives alongside the fetcher code (not in ``assets/``)
        so it ships inside the wheel and is available after ``pip install``
        without requiring git or LFS.  In development,
        ``uv run atlantis setup`` ensures it is present.
        """
        return Path(__file__).with_name("data") / "viirs_aois.geojson"

    def _load_aois(self) -> gpd.GeoDataFrame:
        """Load the packaged VIIRS AOI grid."""
        if not self.aoi_path.exists():
            raise FileNotFoundError(
                f"VIIRS AOI grid not found at {self.aoi_path}\n"
                "Run the bootstrap setup to restore it:\n"
                "  uv run atlantis setup\n"
                "  or: uv run python scripts/setup.py"
            )
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

    def _materialize_tile(self, source_path: Path, output_dir: Path) -> Path:
        """Return a TIFF tile path for downstream raster processing."""
        if source_path.suffix.lower() == ".zip":
            return self._extract_tif(source_path, output_dir)
        return source_path

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
            location = self.backend.get_listing_location(self.base_url, event_date, self.data_format)
            entries = self.backend.get_directory_links(self.base_url, location.locator, self.timeout)
            if not entries:
                continue

            for row in aois.itertuples():
                filename = self.backend.find_remote_filename(int(row.AOI_ID), entries)
                if filename is None:
                    continue
                bounds = row.geometry.bounds
                results.append(
                    SearchResult(
                        source_id=self.source_id,
                        item_id=f"viirs:{location.date_token}:{int(row.AOI_ID):03d}",
                        timestamp=event_date,
                        bbox=(bounds[0], bounds[1], bounds[2], bounds[3]),
                        url=self.backend.build_result_url(self.base_url, location.locator, filename),
                        properties={
                            "aoi_id": int(row.AOI_ID),
                            "date": location.date_token,
                            "filename": filename,
                            "backend": self.backend_name,
                            "format": self.data_format,
                        },
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
        raw_dir = ensure_dir(output_dir / "raw") if not self.stream else output_dir / "raw"
        processed_dir = output_dir / "processed"
        if self.keep_processed:
            processed_dir = ensure_dir(processed_dir)

        # Group results by date
        grouped_results: dict[str, list[SearchResult]] = defaultdict(list)
        for result in search_results:
            grouped_results[result.properties["date"]].append(result)

        area_geom = box(*event.bbox)
        processor = ViirsRasterProcessor(area_geom, classify=self.classify)

        all_processed: list[tuple[str, ProcessTilesResult]] = []

        for date_token, dated_results in sorted(grouped_results.items()):
            if self.stream:
                # ── Streaming mode: pass remote URLs directly to processor ──
                tile_sources: list[Path | str] = [
                    result.url for result in sorted(dated_results, key=lambda item: item.properties["aoi_id"])
                ]
                if not tile_sources:
                    continue
                process_result = processor.process_tiles(
                    tile_sources,
                    event.event_id,
                    date_token,
                    processed_dir,
                    write_outputs=self.keep_processed,
                )
            else:
                # ── Download mode (default): download tiles, then process ──
                tile_paths_local = []
                for result in sorted(dated_results, key=lambda item: item.properties["aoi_id"]):
                    filename = result.properties["filename"]
                    download_path = raw_dir / filename
                    download_file(result.url, output_path=download_path)
                    tile_paths_local.append(self._materialize_tile(download_path, raw_dir))

                if not tile_paths_local:
                    continue
                process_result = processor.process_tiles(
                    tile_paths_local,
                    event.event_id,
                    date_token,
                    processed_dir,
                    write_outputs=self.keep_processed,
                )

            if process_result is not None:
                all_processed.append((date_token, process_result))

        if not all_processed:
            return []

        # ── Strategy dispatch ──────────────────────────────────────────
        if self.strategy == "all":
            if self.keep_processed:
                return [self._fetch_result_from_process(event.event_id, dt, pr) for dt, pr in all_processed]
            else:
                # Multi-date in-memory dataset
                return [self._multi_date_fetch_result(event.event_id, all_processed)]

        elif self.strategy == "aggregate":
            all_tiles = [pr.processed for _, pr in all_processed]
            aggregated = ViirsRasterProcessor.aggregate_tiles(all_tiles)
            # If keep_processed, we could write the aggregate, but for now just return in-memory
            return [
                self._in_memory_fetch_result(
                    event.event_id,
                    "aggregated",
                    ProcessTilesResult(OutputPaths(), all_processed[0][1].metadata, aggregated),
                )
            ]

        else:  # peak
            best_date_token, best_result = max(all_processed, key=lambda item: flood_pixel_count(item[1].processed))
            if self.keep_processed:
                return [self._fetch_result_from_process(event.event_id, best_date_token, best_result)]
            else:
                return [self._in_memory_fetch_result(event.event_id, best_date_token, best_result)]

    @staticmethod
    def _fetch_result_from_process(event_id: str, date_token: str, process_result: ProcessTilesResult) -> FetchResult:
        paths = process_result.paths
        return FetchResult(
            event_id=event_id,
            source_id="viirs",
            files=[
                p for p in (paths.raw, paths.flood_fraction, paths.quality_mask, paths.permanent_water) if p is not None
            ],
            metadata=process_result.metadata,
            date_token=date_token,
        )

    def _in_memory_fetch_result(
        self,
        event_id: str,
        date_token: str,
        process_result: ProcessTilesResult,
    ) -> FetchResult:
        dataset = processed_tile_to_dataset(
            process_result.processed,
            event_id=event_id,
            source_id=self.source_id,
        )
        return FetchResult(
            event_id=event_id,
            source_id=self.source_id,
            files=[],
            metadata=process_result.metadata,
            date_token=date_token,
            dataset=dataset,
        )

    def to_dataset(self, result: FetchResult) -> "xr.Dataset":
        """Convert VIIRS fetch result to xarray Dataset.

        Args:
            result: The fetch result to convert.

        Returns:
            xarray Dataset with VIIRS data.
        """
        try:
            import rioxarray as rxr
            import xarray as xr
        except ImportError as exc:
            raise ImportError("rioxarray and xarray are required to read VIIRS datasets") from exc

        if result.dataset is not None:
            return result.dataset

        files_by_name = {path.name: path for path in result.files}

        raw_path = next((path for name, path in files_by_name.items() if name.endswith("_raw.tif")), None)

        variables = {}
        if raw_path:
            variables["raw"] = rxr.open_rasterio(raw_path).squeeze(drop=True).rename("raw")
        else:
            obs_path = next(path for name, path in files_by_name.items() if name.endswith("_flood_fraction.tif"))
            quality_path = next(path for name, path in files_by_name.items() if name.endswith("_quality_mask.tif"))
            permanent_water_path = next(
                path for name, path in files_by_name.items() if name.endswith("_permanent_water.tif")
            )

            variables["flood_fraction"] = (
                rxr.open_rasterio(obs_path).squeeze(drop=True).astype("float32") / 100.0
            ).rename("flood_fraction")
            variables["quality_mask"] = (
                rxr.open_rasterio(quality_path).squeeze(drop=True).astype("uint8").rename("quality_mask")
            )
            variables["permanent_water"] = (
                rxr.open_rasterio(permanent_water_path).squeeze(drop=True).astype("uint8").rename("permanent_water")
            )

        dataset = xr.Dataset(variables)
        dataset.attrs["source_id"] = self.source_id
        dataset.attrs["event_id"] = result.event_id
        return dataset
