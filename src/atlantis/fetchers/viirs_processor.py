"""Raster processing for VIIRS flood data.

This module encapsulates the raster operations (mosaic, clip, classify, write)
that were previously mixed into the VIIRSFetcher.fetch() method.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.mask import mask
from rasterio.merge import merge
from shapely.geometry.base import BaseGeometry

from atlantis.models.metadata import TileMetadata

if TYPE_CHECKING:
    from rasterio.io import DatasetReader


FLOOD_MIN_CODE = 160
CLOUD_CODES = {30}
PERMANENT_WATER_CODES = {17}
SEASONAL_WATER_CODES = {20}
OPEN_WATER_CODES = {99}


@dataclass(frozen=True)
class ProcessedTile:
    """Result from processing VIIRS tiles for a single date.

    Attributes:
        flood_extent: Binary flood extent array (0/1).
        quality_mask: Quality mask array (0=bad, 1=good).
        permanent_water: Permanent water mask array (0/1).
        transform: Affine transform for the processed data.
        crs: Coordinate reference system.
        cloud_fraction: Fraction of cloud cover (0.0-1.0).
    """

    flood_extent: np.ndarray
    quality_mask: np.ndarray
    permanent_water: np.ndarray
    transform: rasterio.Affine
    crs: str
    cloud_fraction: float


@dataclass(frozen=True)
class OutputPaths:
    """Paths for the three output files."""

    flood_extent: Path
    quality_mask: Path
    permanent_water: Path


class ViirsRasterProcessor:
    """Processor for VIIRS raster operations.

    This class encapsulates the raster processing pipeline:
    1. Mosaic multiple tiles
    2. Clip to area of interest
    3. Classify pixels (flood, cloud, permanent water, etc.)
    4. Write output GeoTIFFs

    Attributes:
        area_geometry: The geometry to clip results to.
        crs: Target CRS (default: EPSG:4326).
    """

    def __init__(self, area_geometry: BaseGeometry, crs: str = "EPSG:4326") -> None:
        """Initialize the processor.

        Args:
            area_geometry: The geometry to clip results to.
            crs: Target CRS for outputs.
        """
        self.area_geometry = area_geometry
        self.crs = crs

    def process_tiles(
        self, tile_paths: list[Path], event_id: str, date_token: str, output_dir: Path
    ) -> tuple[OutputPaths, TileMetadata] | None:
        """Process a group of tiles for a single date.

        Args:
            tile_paths: List of paths to source TIFF tiles.
            event_id: The flood event identifier.
            date_token: Date string token for filenames.
            output_dir: Directory to write output files.

        Returns:
            Tuple of (output paths, metadata) if successful, None if no tiles.
        """
        if not tile_paths:
            return None

        processed = self._mosaic_and_clip(tile_paths)
        if processed is None:
            return None

        base_name = f"{event_id}_{date_token}_viirs"
        paths = OutputPaths(
            flood_extent=output_dir / f"{base_name}_flood_extent.tif",
            quality_mask=output_dir / f"{base_name}_quality_mask.tif",
            permanent_water=output_dir / f"{base_name}_permanent_water.tif",
        )

        self._write_outputs(processed, paths)

        metadata = self._build_metadata(
            event_id=event_id,
            processed=processed,
            width=processed.flood_extent.shape[1],
            height=processed.flood_extent.shape[0],
        )

        return paths, metadata

    def _mosaic_and_clip(self, tile_paths: list[Path]) -> ProcessedTile | None:
        """Mosaic tiles and clip to the area of interest.

        Args:
            tile_paths: List of paths to source TIFF tiles.

        Returns:
            ProcessedTile with classified arrays, or None if processing fails.
        """
        srcs: list[DatasetReader] = []
        try:
            srcs = [rasterio.open(tile_path) for tile_path in tile_paths]
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

        return self._classify_pixels(clipped[0], clipped_transform, str(crs_value))

    def _classify_pixels(self, data: np.ndarray, transform: rasterio.Affine, crs: str) -> ProcessedTile:
        """Classify pixel values into flood extent, quality mask, and permanent water.

        Args:
            data: Raw pixel values from the clipped raster.
            transform: Affine transform for the data.
            crs: Coordinate reference system.

        Returns:
            ProcessedTile with classified arrays.
        """
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

        cloud_fraction = float(cloud.sum() / cloud.size) if cloud.size else 0.0

        return ProcessedTile(
            flood_extent=flood_extent,
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
        output_meta = {
            "driver": "GTiff",
            "height": processed.flood_extent.shape[0],
            "width": processed.flood_extent.shape[1],
            "count": 1,
            "dtype": "uint8",
            "crs": processed.crs,
            "transform": processed.transform,
            "nodata": 0,
            "compress": "LZW",
        }

        for path, array in (
            (paths.flood_extent, processed.flood_extent),
            (paths.quality_mask, processed.quality_mask),
            (paths.permanent_water, processed.permanent_water),
        ):
            with rasterio.open(path, "w", **output_meta) as dst:
                dst.write(array, 1)

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
