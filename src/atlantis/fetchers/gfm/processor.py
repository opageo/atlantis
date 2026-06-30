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

#: Nominal GFM ground sample distance (metres) — used to size the processed grid.
GFM_NATIVE_GSD_M: float = 20.0

#: Nominal metres per degree of latitude/longitude at the equator.
_METERS_PER_DEGREE: float = 111_320.0

#: STAC configuration for odc.stac.load — marks nodata = 255.
GFM_STAC_CFG: dict = {
    "GFM": {
        "assets": {
            "ensemble_flood_extent": {"data_type": "uint8", "nodata": GFM_NODATA},
            "reference_water_mask": {"data_type": "uint8", "nodata": GFM_NODATA},
        }
    }
}


def _masked_max(a: np.ndarray, b: np.ndarray, nodata: int) -> np.ndarray:
    """Element-wise max of two uint8 arrays treating *nodata* as absent.

    A valid code (anything other than *nodata*) always beats a nodata value.
    When both pixels are valid the numeric maximum is returned.  When both
    are nodata the result is nodata.

    Args:
        a: First uint8 array.
        b: Second uint8 array.
        nodata: Sentinel value marking missing / no-data pixels.

    Returns:
        uint8 array of the same shape as *a* / *b*.
    """
    a_valid = a != nodata
    b_valid = b != nodata
    result = np.full_like(a, nodata)
    # Both valid → numeric max
    both = a_valid & b_valid
    result = np.where(both, np.maximum(a, b), result)
    # Only a is valid → keep a
    result = np.where(a_valid & ~b_valid, a, result)
    # Only b is valid → keep b
    result = np.where(~a_valid & b_valid, b, result)
    return result.astype(np.uint8)


@dataclass(frozen=True)
class GfmProcessedTile:
    """Result from processing GFM items for a single date group.

    Classified mode (``classify=True``, default):
        flood_fraction: Float32 array [0, 1] — fraction of observations with flood.
        quality_mask: Uint8 array (1=valid observation exists, 0=no data).
        permanent_water: Uint8 array (1=permanent water, 0=not).

    Native / raw mode (``classify=False``):
        ensemble_flood_extent: Uint8 array of raw codes (0=dry,1=flood,255=nodata),
            max-pooled across items for the date group and reprojected to the
            ~80 m processed grid with nearest-neighbour resampling.
        reference_water_mask: Uint8 array of raw codes (0=land,1=water,2=perm,255=nodata),
            same treatment.

    Common fields:
        transform: Affine transform for the output grid.
        crs: Coordinate reference system string (e.g. "EPSG:4326").
        shape: (height, width) of the output arrays.
        cloud_fraction: Fraction of pixels with no data (proxy for coverage).
    """

    transform: "Affine"
    crs: str
    shape: tuple[int, int]
    cloud_fraction: float = 0.0
    # Classified fields
    flood_fraction: np.ndarray | None = None
    quality_mask: np.ndarray | None = None
    permanent_water: np.ndarray | None = None
    # Native / raw fields
    ensemble_flood_extent: np.ndarray | None = None
    reference_water_mask: np.ndarray | None = None

    @property
    def is_classified(self) -> bool:
        """True when derived layers are present rather than the native bands."""
        return self.flood_fraction is not None


@dataclass(frozen=True)
class GfmOutputPaths:
    """File paths for written GFM processed outputs."""

    # Classified paths
    flood_fraction: Path | None = None
    quality_mask: Path | None = None
    permanent_water: Path | None = None
    # Native / raw paths
    ensemble_flood_extent: Path | None = None
    reference_water_mask: Path | None = None


@dataclass(frozen=True)
class GfmProcessResult:
    """Complete result from the GFM processing pipeline."""

    processed: GfmProcessedTile
    paths: GfmOutputPaths | None
    metadata: TileMetadata


class GfmRasterProcessor:
    """Processes GFM STAC items into flood fraction or native-band maps.

    Classified mode (``classify=True``, default):
    1. Load each item in native CRS at native resolution.
    2. Build per-class 0/1 masks, then mean-pool by the coarsen factor
       (fraction of sub-pixels per class; no categorical ranking).
    3. Reproject to EPSG:4326 aligned to the ~80 m global grid.
    4. Accumulate per-pixel flood/valid/permanent-water counts.
    5. Derive flood_fraction, quality_mask, permanent_water.

    Native / raw mode (``classify=False``):
    1. Load each item in native CRS at native resolution.
    2. Reproject raw codes to EPSG:4326 using nearest-neighbour (no coarsen-avg).
    3. Max-pool codes across items for the same date group.
    4. Emit ensemble_flood_extent and reference_water_mask as-is.
    """

    def __init__(
        self,
        bbox: tuple[float, float, float, float],
        coarsen_factor: int = DEFAULT_COARSEN_FACTOR,
        resampling: Resampling = Resampling.average,
        reprojector: Reprojector | None = None,
        classify: bool = True,
    ) -> None:
        """Initialize the GFM raster processor.

        Args:
            bbox: Bounding box as (west, south, east, north).
            coarsen_factor: Spatial coarsening factor before reprojection.
                Ignored when *classify* is False.
            resampling: Resampling method for reprojection to EPSG:4326.
                Ignored when *classify* is False (nearest-neighbour used instead).
            reprojector: Pre-configured Reprojector instance. If None, one is
                created at the coarsen-applied native resolution (~80 m for
                coarsen_factor=4), snapped to the global grid.
            classify: When True (default), derive flood_fraction / quality_mask /
                permanent_water from per-pixel counts. When False, emit the native
                ensemble_flood_extent and reference_water_mask bands as-is,
                reprojected with nearest-neighbour to the ~80 m processed grid.
                The downstream ``--harmonise`` step resamples processed/ to the
                canonical 1-arcmin grid (matching VIIRS/MODIS behaviour).
        """
        self.bbox = bbox
        self.coarsen_factor = coarsen_factor
        self.resampling = resampling
        self.classify = classify
        # GFM processed/ is written at the coarsen-applied native resolution
        # (~80 m for coarsen_factor=4), expressed in degrees. The downstream
        # --harmonise step resamples this to the canonical 1-arcmin grid, so GFM
        # behaves like VIIRS/MODIS (source-res processed → 1-arcmin harmonised).
        processed_resolution = (GFM_NATIVE_GSD_M * coarsen_factor) / _METERS_PER_DEGREE
        self.reprojector = reprojector or Reprojector(
            target_crs="EPSG:4326",
            target_resolution=processed_resolution,
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
        """Process a list of STAC items into a flood map.

        In classified mode (``classify=True``, default), derives
        ``flood_fraction`` / ``quality_mask`` / ``permanent_water`` from
        per-pixel accumulator counts.  In native mode (``classify=False``),
        reprojects raw band codes to the ~80 m processed grid using
        nearest-neighbour and max-pools codes across items for the date group.

        Args:
            items: List of pystac Items to process.
            event_id: Flood event identifier.
            date_token: Date string (YYYYMMDD) for this batch.
            output_dir: Directory for writing output files.
            write_outputs: Whether to write GeoTIFFs to disk.

        Returns:
            GfmProcessResult or None if no valid data was found.
        """
        if not items:
            return None
        if self.classify:
            return self._process_items_classified(
                items,
                event_id=event_id,
                date_token=date_token,
                output_dir=output_dir,
                write_outputs=write_outputs,
            )
        return self._process_items_native(
            items,
            event_id=event_id,
            date_token=date_token,
            output_dir=output_dir,
            write_outputs=write_outputs,
        )

    def _process_items_classified(
        self,
        items: list,
        *,
        event_id: str = "",
        date_token: str = "",
        output_dir: Path | None = None,
        write_outputs: bool = True,
    ) -> GfmProcessResult | None:
        """Classified processing path: coarsen → accumulate → derive products."""
        import odc.stac
        import pyproj
        import rioxarray  # noqa: F401
        import xarray as xr
        from shapely.geometry import box

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

            # Build per-class 0/1 masks at native resolution, then mean-pool to
            # the coarsened grid. Mean-pooling a binary mask yields the fraction
            # of sub-pixels in each class — the correct way to downsample nominal
            # codes, and consistent with the average reproject that follows. A
            # categorical max would rank codes by number (and let nodata=255 win
            # every mixed block), which is meaningless for class labels.
            flood_mask_native, perm_mask_native, valid_mask_native = self._build_native_masks(
                xx["ensemble_flood_extent"],
                xx["reference_water_mask"],
                self.coarsen_factor,
            )

            masks = xr.Dataset(
                {
                    "flood": flood_mask_native,
                    "perm": perm_mask_native,
                    "valid": valid_mask_native,
                }
            ).rio.write_crs(crs_src)

            # Reproject each mask directly onto the ~80 m global
            # grid (pre-computed snapped bounds/transform). This ensures all
            # items accumulate on the same aligned grid — no double
            # reprojection needed at harmonisation time.
            masks_ll = self._reproject_to_canonical_grid(masks)
            del xx, flood_mask_native, perm_mask_native, valid_mask_native, masks

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

    def _process_items_native(
        self,
        items: list,
        *,
        event_id: str = "",
        date_token: str = "",
        output_dir: Path | None = None,
        write_outputs: bool = True,
    ) -> GfmProcessResult | None:
        """Native / raw processing path: NN-reproject codes and max-pool across items."""
        import odc.stac
        import pyproj
        import rioxarray  # noqa: F401
        from shapely.geometry import box

        first_item = items[0]
        crs_src = pyproj.CRS.from_wkt(first_item.properties["proj:wkt2"])
        resolution = first_item.properties["gsd"]

        west, south, east, north = self.bbox
        aoi = box(west, south, east, north)

        # Accumulators: masked-max of codes across items (nodata=255)
        efe_accum: np.ndarray | None = None
        rwm_accum: np.ndarray | None = None

        logger.info(
            "Processing {} GFM items in native mode (nearest-neighbour reproject, no coarsen)",
            len(items),
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

            # Reproject raw codes to the ~80 m processed grid with NN;
            # codes are discrete so continuous resampling would corrupt them.
            codes_ds = xr.Dataset(
                {
                    "ensemble_flood_extent": xx["ensemble_flood_extent"].squeeze(drop=True),
                    "reference_water_mask": xx["reference_water_mask"].squeeze(drop=True),
                }
            ).rio.write_crs(crs_src)
            codes_ll = self._reproject_codes_to_canonical_grid(codes_ds)

            efe = np.squeeze(codes_ll["ensemble_flood_extent"].values).astype(np.uint8)
            rwm = np.squeeze(codes_ll["reference_water_mask"].values).astype(np.uint8)
            del xx, codes_ds, codes_ll

            if efe_accum is None:
                efe_accum = efe.copy()
                rwm_accum = rwm.copy()
            else:
                # Masked max: valid code beats nodata; max of two valid codes wins.
                efe_accum = _masked_max(efe_accum, efe, GFM_NODATA)
                rwm_accum = _masked_max(rwm_accum, rwm, GFM_NODATA)

            logger.debug("Item {}/{} processed (native)", idx + 1, len(items))

        if efe_accum is None or rwm_accum is None:
            logger.warning("No valid data found in {} items", len(items))
            return None

        processed = self._build_native_tile(efe_accum, rwm_accum)

        metadata = TileMetadata(
            event_id=event_id,
            source_id="gfm",
            fetch_timestamp=datetime.now(timezone.utc),
            crs="EPSG:4326",
            resolution=self.reprojector.target_resolution,
            bbox=self._snapped_bounds,
            cloud_fraction=processed.cloud_fraction,
            permanent_water_mask_available=False,
        )

        paths: GfmOutputPaths | None = None
        if write_outputs and output_dir is not None:
            paths = self._write_outputs(processed, event_id, date_token, output_dir)

        return GfmProcessResult(processed=processed, paths=paths, metadata=metadata)

    @staticmethod
    def _build_native_masks(
        flood_native: "xr.DataArray",
        perm_native: "xr.DataArray",
        coarsen_factor: int = 1,
    ) -> tuple["xr.DataArray", "xr.DataArray", "xr.DataArray"]:
        """Build float32 flood, permanent-water, and validity coverage masks.

        The discrete GFM source codes are nominal categories (flood / dry /
        nodata; land / water / permanent / nodata), so they must not be pooled
        by numeric rank. Each code is first turned into a 0/1 mask at native
        resolution, then optionally **mean-pooled** by ``coarsen_factor`` —
        yielding the *fraction* of sub-pixels in each coarsened cell that belong
        to the class. Mean-pooling a 0/1 mask is the correct categorical
        downsampling and is consistent with the ``average`` reprojection applied
        afterwards; a categorical ``max`` would impose a meaningless code
        ordering and let nodata (255) dominate any mixed block.
        """
        flood_mask = (flood_native == GFM_FLOOD).astype("float32")
        perm_mask = (perm_native == GFM_PERMANENT_WATER).astype("float32")
        # An observation contributes to "valid" if either band has a non-nodata code.
        valid_mask = ((flood_native != GFM_NODATA) | (perm_native != GFM_NODATA)).astype("float32")
        if coarsen_factor > 1:
            flood_mask = flood_mask.coarsen(y=coarsen_factor, x=coarsen_factor, boundary="trim").mean()
            perm_mask = perm_mask.coarsen(y=coarsen_factor, x=coarsen_factor, boundary="trim").mean()
            valid_mask = valid_mask.coarsen(y=coarsen_factor, x=coarsen_factor, boundary="trim").mean()
        return flood_mask, perm_mask, valid_mask

    def _reproject_to_canonical_grid(self, masks: "xr.Dataset") -> "xr.Dataset":
        """Reproject a native-CRS mask dataset onto the pre-computed canonical grid.

        Uses rioxarray's ``rio.reproject`` (which correctly handles source
        transforms from odc.stac-loaded data) but forces the destination grid
        to the ~80 m snapped bounds/transform.

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

    def _reproject_codes_to_canonical_grid(self, codes: "xr.Dataset") -> "xr.Dataset":
        """Reproject native uint8 code bands to the canonical grid using nearest-neighbour.

        Nearest-neighbour preserves discrete pixel codes (0/1/2/255) without
        introducing interpolated intermediate values.

        Args:
            codes: xarray Dataset with uint8 band variables and a CRS written
                via rioxarray (``codes.rio.crs``).

        Returns:
            xarray Dataset on the canonical grid with the same variable names.
        """
        codes = codes.squeeze(drop=True)
        return codes.rio.reproject(
            "EPSG:4326",
            nodata=GFM_NODATA,
            resampling=Resampling.nearest,
            shape=(self._dst_height, self._dst_width),
            transform=self._dst_transform,
        )

    def _build_native_tile(
        self,
        efe: np.ndarray,
        rwm: np.ndarray,
    ) -> GfmProcessedTile:
        """Build a GfmProcessedTile holding native band arrays.

        Args:
            efe: ensemble_flood_extent uint8 array on the canonical grid.
            rwm: reference_water_mask uint8 array on the canonical grid.

        Returns:
            GfmProcessedTile with native fields populated.
        """
        total_pixels = efe.size
        valid_pixels = int(np.sum(efe != GFM_NODATA))
        cloud_fraction = 1.0 - (valid_pixels / total_pixels) if total_pixels > 0 else 1.0
        return GfmProcessedTile(
            ensemble_flood_extent=efe,
            reference_water_mask=rwm,
            transform=self._dst_transform,
            crs="EPSG:4326",
            shape=efe.shape,
            cloud_fraction=cloud_fraction,
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

    def write_processed(
        self,
        processed: GfmProcessedTile,
        event_id: str,
        date_token: str,
        output_dir: Path,
    ) -> GfmOutputPaths:
        """Write processed arrays to GeoTIFF files (public wrapper).

        Used by the fetcher to defer writing processed/ GeoTIFFs until after
        peak-window filtering, so only surviving dates are persisted.
        """
        return self._write_outputs(processed, event_id, date_token, output_dir)

    def _write_outputs(
        self,
        processed: GfmProcessedTile,
        event_id: str,
        date_token: str,
        output_dir: Path,
    ) -> GfmOutputPaths:
        """Write processed arrays to GeoTIFF files.

        Writes classified layers (flood_fraction, quality_mask, permanent_water)
        or native bands (ensemble_flood_extent, reference_water_mask) depending
        on which fields are populated in *processed*.
        """
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
                "compress": "LZW",
            }
            with rasterio.open(str(path), "w", **profile) as dst:
                write_data = arr.copy()
                if dtype == "float32":
                    logger.info("Replacing NaN with nodata value {} for {}", nodata, name)
                    write_data = np.where(np.isnan(write_data), nodata, write_data).astype(np.float32)
                dst.write(write_data, 1)
            return path

        # Native / raw mode
        if processed.ensemble_flood_extent is not None:
            efe_path = _write_tif(processed.ensemble_flood_extent, "ensemble_flood_extent", "uint8", GFM_NODATA)
            rwm_path = _write_tif(processed.reference_water_mask, "reference_water_mask", "uint8", GFM_NODATA)
            return GfmOutputPaths(ensemble_flood_extent=efe_path, reference_water_mask=rwm_path)

        # Classified mode — flood_fraction as uint8 percent (0–100) nodata 255,
        # mirroring the VIIRS/MODIS convention (was float32 / -9999).
        ff_src = np.squeeze(processed.flood_fraction)
        ff_pct = np.full(ff_src.shape, GFM_NODATA, dtype=np.uint8)
        ff_valid = np.isfinite(ff_src)
        ff_pct[ff_valid] = np.round(np.clip(ff_src[ff_valid], 0.0, 1.0) * 100).astype(np.uint8)
        ff_path = _write_tif(ff_pct, "flood_fraction", "uint8", GFM_NODATA)
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

        Classified mode: mean for flood_fraction, OR for quality, majority for
        permanent_water.
        Native mode: masked-max of raw codes across all dates.
        """
        if not tiles:
            return None
        if len(tiles) == 1:
            return tiles[0]

        ref = tiles[0]

        # ── Native / raw mode ─────────────────────────────────────────────────
        if ref.ensemble_flood_extent is not None:
            efe = ref.ensemble_flood_extent.copy()
            rwm = ref.reference_water_mask.copy()
            for t in tiles[1:]:
                efe = _masked_max(efe, t.ensemble_flood_extent, GFM_NODATA)
                rwm = _masked_max(rwm, t.reference_water_mask, GFM_NODATA)
            total_pixels = efe.size
            valid_pixels = int(np.sum(efe != GFM_NODATA))
            cloud_fraction = 1.0 - (valid_pixels / total_pixels) if total_pixels > 0 else 1.0
            return GfmProcessedTile(
                ensemble_flood_extent=efe,
                reference_water_mask=rwm,
                transform=ref.transform,
                crs=ref.crs,
                shape=ref.shape,
                cloud_fraction=cloud_fraction,
            )

        # ── Classified mode ────────────────────────────────────────────────────
        # Stack flood fractions and compute mean (ignoring NaN)
        ff_stack = np.stack([t.flood_fraction for t in tiles], axis=0)
        flood_fraction = np.nanmean(ff_stack, axis=0).astype(np.float32)

        # Quality: OR across dates (1 if any date had valid data)
        qm_stack = np.stack([t.quality_mask for t in tiles], axis=0)
        quality_mask = np.any(qm_stack > 0, axis=0).astype(np.uint8)

        # Permanent water: majority vote across dates
        pw_stack = np.stack([t.permanent_water for t in tiles], axis=0)
        permanent_water = (np.mean(pw_stack, axis=0) > 0.5).astype(np.uint8)

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
