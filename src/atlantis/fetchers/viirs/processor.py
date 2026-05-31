"""Raster processing for VIIRS flood data.

This module encapsulates the raster operations (mosaic, clip, classify, write)
that were previously mixed into the VIIRSFetcher.fetch() method.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Union

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.mask import mask
from rasterio.merge import merge
from shapely.geometry.base import BaseGeometry

from atlantis.models.metadata import TileMetadata

if TYPE_CHECKING:
    from rasterio.io import DatasetReader


# Type alias: a tile location can be a local file path or a /vsicurl/ URL
TilePath = Union[Path, str]

FLOOD_MIN_CODE = 160  #: conservative default: ≥60% water fraction
FILL_CODES = {0, 1}
CLOUD_CODES = {30}
PERMANENT_WATER_CODES = {17}
SEASONAL_WATER_CODES = {20}
OPEN_WATER_CODES = {99}

_VSICURL_PREFIX = "/vsicurl/"


def _resolve_tile_path(item: TilePath) -> str:
    """Convert a tile location to a rasterio-compatible path string.

    - If it's a remote URL (https://), prepend ``/vsicurl/``.
    - If it's a local ``Path``, return its str form.
    """
    if isinstance(item, str):
        if item.startswith(("https://", "http://")):
            if not item.startswith(_VSICURL_PREFIX):
                return _VSICURL_PREFIX + item
            return item
        return item
    return str(item)


@dataclass(frozen=True)
class ProcessedTile:
    """Result from processing VIIRS tiles for a single date.

    Attributes:
        raw: Raw tile data.
        flood_fraction: Continuous flood fraction array (float32, 0.0–1.0).
            Derived from VIIRS water-fraction codes 101–200 as (code−100)/100.
            Non-flood codes produce 0.0.
        quality_mask: Quality mask array (0=bad, 1=good).
        permanent_water: Permanent water mask array (0/1).
        transform: Affine transform for the processed data.
        crs: Coordinate reference system.
        cloud_fraction: Fraction of cloud cover (0.0-1.0).
    """

    transform: rasterio.Affine
    crs: str
    cloud_fraction: float
    raw: np.ndarray | None = None
    flood_fraction: np.ndarray | None = None
    quality_mask: np.ndarray | None = None
    permanent_water: np.ndarray | None = None


@dataclass(frozen=True)
class OutputPaths:
    """Paths for the output files."""

    raw: Path | None = None
    flood_fraction: Path | None = None
    quality_mask: Path | None = None
    permanent_water: Path | None = None


@dataclass(frozen=True)
class ProcessTilesResult:
    """Outcome of processing VIIRS tiles for one date."""

    paths: OutputPaths
    metadata: TileMetadata
    processed: ProcessedTile


class ViirsRasterProcessor:
    """Processor for VIIRS raster operations.

    This class encapsulates the raster processing pipeline:
    1. Mosaic multiple tiles
    2. Clip to area of interest
    3. Classify pixels (flood, cloud, permanent water, etc.)
    4. Write output GeoTIFFs

    Supports both local file paths and ``/vsicurl/`` remote URLs.

    Attributes:
        area_geometry: The geometry to clip results to.
        crs: Target CRS (default: EPSG:4326).
    """

    def __init__(
        self,
        area_geometry: BaseGeometry,
        crs: str = "EPSG:4326",
        classify: bool = False,
    ) -> None:
        """Initialize the processor.

        Args:
            area_geometry: The geometry to clip results to.
            crs: Target CRS for outputs.
            classify: Whether to classify pixels into discrete flood layers.
                If False (default), outputs raw data only.
        """
        self.area_geometry = area_geometry
        self.crs = crs
        self.classify = classify

    def process_tiles(
        self,
        tile_paths: list[TilePath],
        event_id: str,
        date_token: str,
        output_dir: Path,
        *,
        write_outputs: bool = True,
    ) -> ProcessTilesResult | None:
        """Process a group of tiles for a single date.

        Args:
            tile_paths: List of source TIFF locations (local ``Path`` or
                ``/vsicurl/``-prefixed remote URL strings).
            event_id: The flood event identifier.
            date_token: Date string token for filenames.
            output_dir: Directory for output path construction (and writes when enabled).
            write_outputs: When False, keep rasters in memory only (no GeoTIFF writes).

        Returns:
            :class:`ProcessTilesResult` if successful, None if no tiles.
        """
        if not tile_paths:
            return None

        processed = self._mosaic_and_clip(tile_paths)
        if processed is None:
            return None

        base_name = f"{event_id}_{date_token}_viirs"
        if self.classify:
            paths = OutputPaths(
                flood_fraction=output_dir / f"{base_name}_flood_fraction.tif",
                quality_mask=output_dir / f"{base_name}_quality_mask.tif",
                permanent_water=output_dir / f"{base_name}_permanent_water.tif",
            )
        else:
            paths = OutputPaths(raw=output_dir / f"{base_name}_raw.tif")

        if write_outputs:
            self._write_outputs(processed, paths)

        metadata = self._build_metadata(
            event_id=event_id,
            processed=processed,
            width=processed.raw.shape[1] if processed.raw is not None else processed.flood_fraction.shape[1],
            height=processed.raw.shape[0] if processed.raw is not None else processed.flood_fraction.shape[0],
        )

        return ProcessTilesResult(paths=paths, metadata=metadata, processed=processed)

    def _mosaic_and_clip(self, tile_paths: list[TilePath]) -> ProcessedTile | None:
        """Mosaic tiles and clip to the area of interest.

        Supports both local ``Path`` objects and ``/vsicurl/`` remote URLs.

        Args:
            tile_paths: List of source TIFF locations.

        Returns:
            ProcessedTile with classified arrays, or None if processing fails.
        """
        srcs: list[DatasetReader] = []
        try:
            resolved = [_resolve_tile_path(p) for p in tile_paths]
            srcs = [rasterio.open(rp) for rp in resolved]
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
                clipped, clipped_transform = mask(dataset, [self.area_geometry], crop=True)

        # Convert CRS to string if it's a rasterio CRS object
        crs_value = meta.get("crs", self.crs)
        if hasattr(crs_value, "to_string"):
            crs_value = crs_value.to_string()
        elif crs_value is None:
            crs_value = self.crs

        if self.classify:
            return self._classify_pixels(clipped[0], clipped_transform, str(crs_value))
        else:
            return ProcessedTile(
                raw=clipped[0],
                transform=clipped_transform,
                crs=str(crs_value),
                cloud_fraction=0.0,
            )

    def _classify_pixels(self, data: np.ndarray, transform: rasterio.Affine, crs: str) -> ProcessedTile:
        """Classify pixel values into flood extent, quality mask, and permanent water.

        Args:
            data: Raw pixel values from the clipped raster.
            transform: Affine transform for the data.
            crs: Coordinate reference system.

        Returns:
            ProcessedTile with classified arrays.
        """
        fill = np.isin(data, list(FILL_CODES))
        cloud = np.isin(data, list(CLOUD_CODES))
        permanent_water = np.isin(data, list(PERMANENT_WATER_CODES))

        # Continuous flood fraction: codes 101–200 encode water fraction as (code−100)/100.
        # All other codes (including non-flood water classes) produce 0.0.
        flood_mask = (data >= 101) & (data <= 200)
        flood_fraction = np.where(
            flood_mask,
            (data.astype(np.float32) - 100.0) / 100.0,
            np.float32(0.0),
        )

        # Quality mask: 1 = valid clear-sky observation, 0 = unusable (fill or cloud).
        # Pre-existing water types (permanent/seasonal/open) are valid observations —
        # exclude them via the permanent_water mask, not here.
        quality_mask = np.ones_like(data, dtype=np.uint8)
        quality_mask[fill | cloud] = 0

        permanent_water_mask = np.zeros_like(data, dtype=np.uint8)
        permanent_water_mask[permanent_water] = 1

        # Cloud fraction over non-fill pixels only (fill pixels are not real observations).
        valid = ~fill
        cloud_fraction = float(cloud[valid].sum() / valid.sum()) if valid.sum() else 0.0

        return ProcessedTile(
            flood_fraction=flood_fraction,
            quality_mask=quality_mask,
            permanent_water=permanent_water_mask,
            transform=transform,
            crs=crs,
            cloud_fraction=cloud_fraction,
        )

    def _write_outputs(self, processed: ProcessedTile, paths: OutputPaths) -> None:
        """Write the processed arrays to GeoTIFF files.

        Args:
            processed: The processed tile data.
            paths: Output file paths.
        """
        # Determine shape from whichever array is available
        ref_shape = processed.raw.shape if processed.raw is not None else processed.flood_fraction.shape
        base_meta = {
            "driver": "GTiff",
            "height": ref_shape[0],
            "width": ref_shape[1],
            "count": 1,
            "crs": processed.crs,
            "transform": processed.transform,
            "compress": "LZW",
        }

        if paths.raw is not None and processed.raw is not None:
            with rasterio.open(paths.raw, "w", **base_meta, dtype="uint8", nodata=0) as dst:
                dst.write(processed.raw, 1)

        if paths.flood_fraction is not None and processed.flood_fraction is not None:
            # Store as uint8 percentage (0–100) to save space: 0 = no flood, 1–100 = fraction × 100.
            pct = np.round(processed.flood_fraction * 100).astype(np.uint8)
            with rasterio.open(paths.flood_fraction, "w", **base_meta, dtype="uint8", nodata=255) as dst:
                dst.write(pct, 1)

        if paths.quality_mask is not None and processed.quality_mask is not None:
            with rasterio.open(paths.quality_mask, "w", **base_meta, dtype="uint8", nodata=0) as dst:
                dst.write(processed.quality_mask, 1)

        if paths.permanent_water is not None and processed.permanent_water is not None:
            with rasterio.open(paths.permanent_water, "w", **base_meta, dtype="uint8", nodata=0) as dst:
                dst.write(processed.permanent_water, 1)

    def _build_metadata(self, event_id: str, processed: ProcessedTile, width: int, height: int) -> TileMetadata:
        """Build TileMetadata from processed tile.

        Args:
            event_id: The flood event identifier.
            processed: The processed tile data.
            width: Width of the processed raster.
            height: Height of the processed raster.

        Returns:
            TileMetadata for the processed tile.
        """
        west = processed.transform.c
        east = processed.transform.c + processed.transform.a * width
        north = processed.transform.f
        south = processed.transform.f + processed.transform.e * height

        return TileMetadata(
            event_id=event_id,
            source_id="viirs",
            fetch_timestamp=datetime.now(timezone.utc),
            crs=processed.crs,
            resolution=abs(processed.transform.a),
            bbox=(min(west, east), min(south, north), max(west, east), max(south, north)),
            cloud_fraction=processed.cloud_fraction,
            quality_bitmask=0,
            permanent_water_mask_available=True,
        )
