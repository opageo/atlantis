"""Raster processing for GFM flood data.

Encapsulates the load → coarsen → reproject → accumulate pipeline
from the reference ``extract_gfm.py`` script.

GFM encoding (verified against EODC STAC COGs):
    ``ensemble_flood_extent``: 0 = dry / observed-not-flooded, 1 = flood,
    255 = nodata.
    ``reference_water_mask``: 0 = land, 1 = water (seasonal/observed),
    2 = permanent water, 255 = nodata.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
import xarray as xr
from loguru import logger
from rasterio.enums import Resampling
from rasterio.transform import Affine, from_bounds

from atlantis.config import HarmoniseConfig
from atlantis.harmoniser.reprojector import Reprojector
from atlantis.models.metadata import TileMetadata

# ── GFM code constants ────────────────────────────────────────────────────────
GFM_NODATA: int = 255
# ensemble_flood_extent codes
GFM_DRY: int = 0
GFM_FLOOD: int = 1
# reference_water_mask codes
GFM_LAND: int = 0
GFM_WATER: int = 1
GFM_PERMANENT_WATER: int = 2

#: Bands loaded from STAC items.
GFM_BANDS: list[str] = ["ensemble_flood_extent", "reference_water_mask"]

#: Default coarsen factor (native ~20 m → ~80 m before reproject).
DEFAULT_COARSEN_FACTOR: int = 4

#: STAC configuration for odc.stac.load — marks nodata = 255.
GFM_STAC_CFG: dict = {
    "GFM": {
        "assets": {
            "ensemble_flood_extent": {"data_type": "uint8", "nodata": GFM_NODATA},
            "reference_water_mask": {"data_type": "uint8", "nodata": GFM_NODATA},
        }
    }
}


@dataclass(frozen=True)
class GfmProcessedTile:
    """Result from processing GFM items for a single date group.

    Attributes:
        flood_fraction: Float32 array [0, 1] — fraction of observations with flood.
        quality_mask: Uint8 array (1=valid observation exists, 0=no data).
        permanent_water: Uint8 array (1=permanent water, 0=not).
        transform: Affine transform for the output grid.
        crs: Coordinate reference system string (e.g. "EPSG:4326").
        shape: (height, width) of the output arrays.
        cloud_fraction: Fraction of pixels with no data (proxy for coverage).
    """

    flood_fraction: np.ndarray
    quality_mask: np.ndarray
    permanent_water: np.ndarray
    transform: "Affine"
    crs: str
    shape: tuple[int, int]
    cloud_fraction: float = 0.0


@dataclass(frozen=True)
class GfmOutputPaths:
    """File paths for written GFM processed outputs."""

    flood_fraction: Path | None = None
    quality_mask: Path | None = None
    permanent_water: Path | None = None


@dataclass(frozen=True)
class GfmProcessResult:
    """Complete result from the GFM processing pipeline."""

    processed: GfmProcessedTile
    paths: GfmOutputPaths | None
    metadata: TileMetadata


class GfmRasterProcessor:
    """Processes GFM STAC items into flood fraction maps.

    Follows the ``extract_subdomain`` approach:
    1. Load each item in native CRS at native resolution.
    2. Coarsen by a factor (max-pool to preserve flood codes).
    3. Reproject to EPSG:4326 aligned to the canonical 1-arcmin global grid.
    4. Accumulate per-pixel flood/valid/permanent-water counts.
    5. Derive flood_fraction, quality_mask, permanent_water.
    """

    def __init__(
        self,
        bbox: tuple[float, float, float, float],
        coarsen_factor: int = DEFAULT_COARSEN_FACTOR,
        resampling: Resampling = Resampling.average,
        reprojector: Reprojector | None = None,
    ) -> None:
        """Initialize the GFM raster processor.

        Args:
            bbox: Bounding box as (west, south, east, north).
            coarsen_factor: Spatial coarsening factor before reprojection.
            resampling: Resampling method for reprojection to EPSG:4326.
            reprojector: Pre-configured Reprojector instance. If None, one is
                created from default HarmoniseConfig (1-arcmin, snapped to
                the canonical global grid).
        """
        self.bbox = bbox
        self.coarsen_factor = coarsen_factor
        self.resampling = resampling
        self.reprojector = reprojector or Reprojector(
            target_crs="EPSG:4326",
            target_resolution=HarmoniseConfig().target_resolution,
            resampling_method=resampling.name,
            snap_to_global_grid=True,
        )

        # Pre-compute the snapped target grid for the bbox
        west, south, east, north = self.bbox
        self._snapped_bounds = self.reprojector._snap_bounds_to_global_grid(west, south, east, north)
        sw, ss, se, sn = self._snapped_bounds
        res = self.reprojector.target_resolution
        self._dst_width = max(1, int(round((se - sw) / res)))
        self._dst_height = max(1, int(round((sn - ss) / res)))
        self._dst_transform = from_bounds(sw, ss, se, sn, self._dst_width, self._dst_height)

    def process_items(
        self,
        items: list,
        *,
        event_id: str = "",
        date_token: str = "",
        output_dir: Path | None = None,
        write_outputs: bool = True,
    ) -> GfmProcessResult | None:
        """Process a list of STAC items into a single flood fraction map.

        Args:
            items: List of pystac Items to process.
            event_id: Flood event identifier.
            date_token: Date string (YYYYMMDD) for this batch.
            output_dir: Directory for writing output files.
            write_outputs: Whether to write GeoTIFFs to disk.

        Returns:
            GfmProcessResult or None if no valid data was found.
        """
        import odc.stac
        import pyproj
        import rioxarray  # noqa: F401
        import xarray as xr
        from shapely.geometry import box

        if not items:
            return None

        # Determine native CRS and resolution from first item
        first_item = items[0]
        crs_src = pyproj.CRS.from_wkt(first_item.properties["proj:wkt2"])
        resolution = first_item.properties["gsd"]

        west, south, east, north = self.bbox
        aoi = box(west, south, east, north)

        # Accumulators
        flood_count: np.ndarray | None = None
        perm_water_count: np.ndarray | None = None
        valid_count: np.ndarray | None = None
        ref_coords = None
        ref_dims = None

        logger.info(
            "Processing {} GFM items (coarsen={}, resampling={})",
            len(items),
            self.coarsen_factor,
            self.resampling,
        )

        for idx, item in enumerate(items):
            try:
                xx = odc.stac.load(
                    [item],
                    bbox=aoi.bounds,
                    crs=crs_src,
                    bands=GFM_BANDS,
                    resolution=resolution,
                    dtype="uint8",
                    groupby="solar_day",
                    chunks={},
                )
            except Exception as e:
                logger.warning("Failed to load item {} ({}): {}", idx, item.id, e)
                continue

            # Coarsen with max-pool (preserves highest code: flood > dry > outside)
            flood_native = (
                xx["ensemble_flood_extent"].coarsen(y=self.coarsen_factor, x=self.coarsen_factor, boundary="trim").max()  # ty:ignore[unresolved-attribute]
            )
            perm_native = (
                xx["reference_water_mask"].coarsen(y=self.coarsen_factor, x=self.coarsen_factor, boundary="trim").max()  # ty:ignore[unresolved-attribute]
            )

            flood_mask_native, perm_mask_native, valid_mask_native = self._build_native_masks(
                flood_native,
                perm_native,
            )

            masks = xr.Dataset(
                {
                    "flood": flood_mask_native,
                    "perm": perm_mask_native,
                    "valid": valid_mask_native,
                }
            ).rio.write_crs(crs_src)

            # Reproject each mask directly onto the canonical 1-arcmin global
            # grid (pre-computed snapped bounds/transform). This ensures all
            # items accumulate on the same aligned grid — no double
            # reprojection needed at harmonisation time.
            masks_ll = self._reproject_to_canonical_grid(masks)
            del xx, flood_native, perm_native, flood_mask_native, perm_mask_native, valid_mask_native, masks

            flood_frac = np.squeeze(masks_ll["flood"].fillna(0.0).values.astype("float32"))
            perm_frac = np.squeeze(masks_ll["perm"].fillna(0.0).values.astype("float32"))
            valid_frac = np.squeeze(masks_ll["valid"].fillna(0.0).values.astype("float32"))

            # Initialize accumulators on first valid load
            if flood_count is None:
                shape = flood_frac.shape
                flood_count = np.zeros(shape, dtype=np.float32)
                perm_water_count = np.zeros(shape, dtype=np.float32)
                valid_count = np.zeros(shape, dtype=np.float32)
                ref_coords = masks_ll["flood"].coords
                ref_dims = masks_ll["flood"].dims

            flood_count += flood_frac
            perm_water_count += perm_frac
            valid_count += valid_frac

            del masks_ll, flood_frac, perm_frac, valid_frac
            logger.debug("Item {}/{} processed", idx + 1, len(items))

        if flood_count is None or valid_count is None:
            logger.warning("No valid data found in {} items", len(items))
            return None

        # Compute derived products
        processed = self._classify(flood_count, perm_water_count, valid_count, ref_coords, ref_dims)

        # Build metadata
        metadata = TileMetadata(
            event_id=event_id,
            source_id="gfm",
            fetch_timestamp=datetime.now(timezone.utc),
            crs="EPSG:4326",
            resolution=self.reprojector.target_resolution,
            bbox=self._snapped_bounds,
            cloud_fraction=processed.cloud_fraction,
            permanent_water_mask_available=True,
        )

        # Write outputs if requested
        paths: GfmOutputPaths | None = None
        if write_outputs and output_dir is not None:
            paths = self._write_outputs(processed, event_id, date_token, output_dir)

        return GfmProcessResult(processed=processed, paths=paths, metadata=metadata)

    @staticmethod
    def _build_native_masks(
        flood_native: "xr.DataArray",
        perm_native: "xr.DataArray",
    ) -> tuple["xr.DataArray", "xr.DataArray", "xr.DataArray"]:
        """Build float32 flood, permanent-water, and validity masks.

        The discrete GFM source codes must be converted to binary masks before
        average reprojection. Once raster averaging runs, class codes are no
        longer recoverable.
        """
        flood_mask_native = (flood_native == GFM_FLOOD).astype("float32")
        perm_mask_native = (perm_native == GFM_PERMANENT_WATER).astype("float32")
        # An observation contributes to "valid" if either band has a non-nodata code.
        valid_mask_native = ((flood_native != GFM_NODATA) | (perm_native != GFM_NODATA)).astype("float32")
        return flood_mask_native, perm_mask_native, valid_mask_native

    def _reproject_to_canonical_grid(self, masks: "xr.Dataset") -> "xr.Dataset":
        """Reproject a native-CRS mask dataset onto the pre-computed canonical grid.

        Uses rioxarray's ``rio.reproject`` (which correctly handles source
        transforms from odc.stac-loaded data) but forces the destination grid
        to the canonical 1-arcmin snapped bounds/transform.

        Args:
            masks: xarray Dataset with float32 binary-mask variables and a
                CRS written via rioxarray (``masks.rio.crs``).

        Returns:
            xarray Dataset on the canonical grid with the same variable names.
        """
        # Squeeze out any singleton time dimension before reprojection
        masks = masks.squeeze(drop=True)

        return masks.rio.reproject(
            "EPSG:4326",
            nodata=np.nan,
            resampling=self.resampling,
            shape=(self._dst_height, self._dst_width),
            transform=self._dst_transform,
        )

    def _classify(
        self,
        flood_count: np.ndarray,
        perm_water_count: np.ndarray,
        valid_count: np.ndarray,
        coords,
        dims,
    ) -> GfmProcessedTile:
        """Compute flood fraction, quality mask, and permanent water from counts.

        The counts are float accumulators of per-pixel class coverage fractions
        (one contribution per item, in ``[0, 1]``).
        """
        # Flood fraction: sum(flood_coverage) / sum(valid_coverage), NaN where no valid obs
        with np.errstate(divide="ignore", invalid="ignore"):
            flood_fraction = np.where(
                valid_count > 0,
                flood_count.astype(np.float32) / valid_count.astype(np.float32),
                np.nan,
            ).astype(np.float32)

        # Quality mask: 1 where at least one valid observation contributed
        quality_mask = (valid_count > 0).astype(np.uint8)

        # Permanent water: > 50% of observed coverage is permanent water
        with np.errstate(divide="ignore", invalid="ignore"):
            perm_ratio = np.where(
                valid_count > 0,
                perm_water_count.astype(np.float32) / valid_count.astype(np.float32),
                0.0,
            )
        permanent_water = (perm_ratio > 0.5).astype(np.uint8)

        # Coverage fraction (proxy for cloud/missing)
        total_pixels = flood_fraction.size
        valid_pixels = int(np.sum(quality_mask))
        cloud_fraction = 1.0 - (valid_pixels / total_pixels) if total_pixels > 0 else 1.0

        # Use the pre-computed canonical grid transform
        shape = flood_fraction.shape
        return GfmProcessedTile(
            flood_fraction=flood_fraction,
            quality_mask=quality_mask,
            permanent_water=permanent_water,
            transform=self._dst_transform,
            crs="EPSG:4326",
            shape=shape,
            cloud_fraction=cloud_fraction,
        )

    def _write_outputs(
        self,
        processed: GfmProcessedTile,
        event_id: str,
        date_token: str,
        output_dir: Path,
    ) -> GfmOutputPaths:
        """Write processed arrays to GeoTIFF files."""
        processed_dir = output_dir / "processed"
        processed_dir.mkdir(parents=True, exist_ok=True)

        # Match VIIRS convention: {event}_{date}_{source}_{layer}.tif
        prefix = f"{event_id}_{date_token}_gfm" if date_token else f"{event_id}_gfm"

        def _write_tif(data: np.ndarray, name: str, dtype: str, nodata) -> Path:
            path = processed_dir / f"{prefix}_{name}.tif"
            arr = np.squeeze(data)
            if arr.ndim != 2:
                raise ValueError(f"Expected 2D array for {name}, got shape {data.shape}")
            height, width = arr.shape
            profile = {
                "driver": "GTiff",
                "height": height,
                "width": width,
                "count": 1,
                "dtype": dtype,
                "crs": processed.crs,
                "transform": processed.transform,
                "nodata": nodata,
                "compress": "LZW",  # TODO GFM check compression standard profile
            }
            with rasterio.open(str(path), "w", **profile) as dst:
                write_data = arr.copy()
                if dtype == "float32":
                    logger.info("Replacing NaN with nodata value {} for {}", nodata, name)
                    write_data = np.where(np.isnan(write_data), nodata, write_data).astype(np.float32)
                dst.write(write_data, 1)
            return path

        ff_path = _write_tif(processed.flood_fraction, "flood_fraction", "float32", -9999.0)
        qm_path = _write_tif(processed.quality_mask, "quality_mask", "uint8", 255)
        pw_path = _write_tif(processed.permanent_water, "permanent_water", "uint8", 255)

        return GfmOutputPaths(
            flood_fraction=ff_path,
            quality_mask=qm_path,
            permanent_water=pw_path,
        )

    @staticmethod
    def aggregate_tiles(tiles: list[GfmProcessedTile]) -> GfmProcessedTile | None:
        """Aggregate multiple date-group tiles into one (for aggregate strategy).

        Uses mean for flood_fraction and OR for quality/permanent-water masks.
        """
        if not tiles:
            return None
        if len(tiles) == 1:
            return tiles[0]

        # Stack flood fractions and compute mean (ignoring NaN)
        ff_stack = np.stack([t.flood_fraction for t in tiles], axis=0)
        flood_fraction = np.nanmean(ff_stack, axis=0).astype(np.float32)

        # Quality: OR across dates (1 if any date had valid data)
        qm_stack = np.stack([t.quality_mask for t in tiles], axis=0)
        quality_mask = np.any(qm_stack > 0, axis=0).astype(np.uint8)

        # Permanent water: majority vote across dates
        pw_stack = np.stack([t.permanent_water for t in tiles], axis=0)
        permanent_water = (np.mean(pw_stack, axis=0) > 0.5).astype(np.uint8)

        # Use transform/shape from first tile (all should be same grid)
        ref = tiles[0]
        total_pixels = flood_fraction.size
        valid_pixels = int(np.sum(quality_mask))
        cloud_fraction = 1.0 - (valid_pixels / total_pixels) if total_pixels > 0 else 1.0

        return GfmProcessedTile(
            flood_fraction=flood_fraction,
            quality_mask=quality_mask,
            permanent_water=permanent_water,
            transform=ref.transform,
            crs=ref.crs,
            shape=ref.shape,
            cloud_fraction=cloud_fraction,
        )
