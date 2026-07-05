"""Raster processing for GFM flood data.

Encapsulates the load → coarsen → reproject → accumulate pipeline
from the reference ``extract_gfm.py`` script.

GFM encoding (verified against EODC STAC COGs):
    ``ensemble_flood_extent``: 0 = dry / observed-not-flooded, 1 = flood,
    255 = nodata.
    ``reference_water_mask`` (GFM PDD Table 20): 0 = no water, 1 = permanent
    water, 2 = seasonal water, 255 = nodata.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

import numpy as np
import rasterio
import xarray as xr
from loguru import logger
from rasterio.enums import Resampling
from rasterio.transform import Affine, from_bounds

# Code constants, native band list, and the layer registry live in ``layers.py``
# (the single source of truth). Re-exported here so existing
# ``from ...gfm.processor import GFM_FLOOD`` style imports keep working.
from atlantis.fetchers.gfm.layers import (  # noqa: F401 — re-exported for backwards compatibility
    ENSEMBLE_FLOOD_EXTENT_COUNT,
    ENSEMBLE_WATER_EXTENT_COUNT,
    GFM_BANDS,
    GFM_DRY,
    GFM_FLOOD,
    GFM_LAND,
    GFM_NODATA,
    GFM_PERMANENT_WATER,
    GFM_WATER,
    REFERENCE_WATER_MASK_CODES,
    VALID_COUNT,
    registry,
)
from atlantis.harmoniser.reprojector import Reprojector
from atlantis.layers import DerivationContext, aggregate_layer
from atlantis.models.metadata import TileMetadata

_T = TypeVar("_T")

# ── GFM processing constants ─────────────────────────────────────────────────

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
            "ensemble_water_extent": {"data_type": "uint8", "nodata": GFM_NODATA},
            "reference_water_mask": {"data_type": "uint8", "nodata": GFM_NODATA},
            "exclusion_mask": {"data_type": "uint8", "nodata": GFM_NODATA},
            "ensemble_likelihood": {"data_type": "uint8", "nodata": GFM_NODATA},
            "advisory_flags": {"data_type": "uint8", "nodata": GFM_NODATA},
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


def _masked_or(a: np.ndarray, b: np.ndarray, nodata: int) -> np.ndarray:
    """Element-wise bitwise OR of two uint8 arrays treating *nodata* as absent."""
    a_valid = a != nodata
    b_valid = b != nodata
    result = np.full_like(a, nodata)
    both = a_valid & b_valid
    result = np.where(both, np.bitwise_or(a, b), result)
    result = np.where(a_valid & ~b_valid, a, result)
    result = np.where(~a_valid & b_valid, b, result)
    return result.astype(np.uint8)


@dataclass(frozen=True)
class GfmProcessedTile:
    """Result from processing GFM items for a single date group.

    Classified mode (``classify=True``, default):
        water_fraction: Float32 array [0, 1] — fraction of observations with water.
        flood_fraction: Float32 array [0, 1] — fraction of observations with flood.
        reference_water: Uint8 array of the native reference-water codes under
            the shared layer name.
        extra_layers: Additional native-code outputs carried alongside the core
            fractions, such as exclusion_mask, ensemble_likelihood, and
            advisory_flags.

    Native / raw mode (``classify=False``):
        ensemble_flood_extent: Uint8 array of raw codes (0=dry,1=flood,255=nodata),
            max-pooled across items for the date group and reprojected to the
            ~80 m processed grid with nearest-neighbour resampling.
        reference_water_mask: Uint8 array of raw codes (0=no water, 1=permanent,
            2=seasonal, 255=nodata; GFM PDD Table 20), same treatment.

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
    water_fraction: np.ndarray | None = None
    flood_fraction: np.ndarray | None = None
    reference_water: np.ndarray | None = None
    extra_layers: dict[str, np.ndarray] = field(default_factory=dict)
    # Native / raw fields
    ensemble_flood_extent: np.ndarray | None = None
    reference_water_mask: np.ndarray | None = None

    @property
    def is_classified(self) -> bool:
        """True when derived layers are present rather than the native bands."""
        return self.water_fraction is not None


@dataclass(frozen=True)
class GfmOutputPaths:
    """File paths for written GFM processed outputs."""

    # Classified paths
    water_fraction: Path | None = None
    flood_fraction: Path | None = None
    reference_water: Path | None = None
    # Native / raw paths
    ensemble_flood_extent: Path | None = None
    reference_water_mask: Path | None = None
    extra: dict[str, Path] = field(default_factory=dict)


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
    5. Derive water_fraction, flood_fraction, and reference_water.

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
        max_retries: int = 3,
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
            classify: When True (default), derive water_fraction / flood_fraction /
                reference_water from per-pixel counts. When False, emit the native
                ensemble_flood_extent and reference_water_mask bands as-is,
                reprojected with nearest-neighbour to the ~80 m processed grid.
                The downstream ``--harmonise`` step resamples processed/ to the
                canonical 1-arcmin grid (matching VIIRS/MODIS behaviour).
            max_retries: Number of retries for transient tile-read failures
                (HTTP errors, timeouts, etc.) before skipping an item.
        """
        self.bbox = bbox
        self.coarsen_factor = coarsen_factor
        self.resampling = resampling
        self.classify = classify
        self.max_retries = max_retries
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

    @staticmethod
    def _is_retryable_read_error(exc: Exception) -> bool:
        """Return True when a raster read failure looks transient."""
        msg = str(exc).lower()
        # EODC's object storage can return 404/500 briefly during outages;
        # treat any explicit HTTP response code as retryable.
        if "http response code" in msg:
            return True
        if any(term in msg for term in ("timed out", "timeout", "connection", "reset", "refused")):
            return True
        return isinstance(exc, (rasterio.errors.RasterioIOError, OSError))

    def _retry_read(
        self,
        operation: "Callable[[], _T]",
        *,
        item_id: str = "",
        context: str = "tile read",
    ) -> _T | None:
        """Run a network-touching raster operation with bounded retries.

        If the operation keeps failing after *max_retries* attempts, logs a
        warning and returns ``None`` so the caller can skip the offending item.
        """
        max_attempts = max(1, self.max_retries + 1)
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return operation()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if not self._is_retryable_read_error(exc) or attempt >= max_attempts:
                    logger.warning(
                        "GFM {} failed for item {} after {}/{} attempt(s): {}",
                        context,
                        item_id,
                        attempt,
                        max_attempts,
                        exc,
                    )
                    logger.info("Skipping GFM item {} due to unrecoverable tile-read failure", item_id)
                    return None
                delay = min(0.25 * (2 ** (attempt - 1)), 2.0)
                logger.warning(
                    "GFM {} retry {}/{} for item {} in {:.2f}s: {}",
                    context,
                    attempt + 1,
                    max_attempts,
                    item_id,
                    delay,
                    exc,
                )
                time.sleep(delay)
        # Defensive fallback (unreachable, but keeps mypy happy).
        logger.warning(
            "GFM {} failed for item {} after {}/{} attempt(s): {}",
            context,
            item_id,
            max_attempts,
            max_attempts,
            last_exc,
        )
        return None

    def _load_item(
        self,
        item,
        aoi,
        crs_src,
        resolution: float,
    ) -> "xr.Dataset | None":
        """Load one STAC item into memory, retrying transient read failures."""
        import odc.stac

        def _do_load() -> "xr.Dataset":
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
            # Force eager load so all HTTP reads happen here, inside the retry.
            return xx.load()

        return self._retry_read(
            _do_load,
            item_id=item.id,
            context="tile load",
        )

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
        ``water_fraction`` / ``flood_fraction`` / ``reference_water`` from
        per-pixel accumulator counts. In native mode (``classify=False``),
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
        water_count: np.ndarray | None = None
        valid_count: np.ndarray | None = None
        reference_water_codes: np.ndarray | None = None
        exclusion_codes: np.ndarray | None = None
        advisory_flags: np.ndarray | None = None
        ensemble_likelihood: np.ndarray | None = None
        ref_coords = None
        ref_dims = None

        logger.info(
            "Processing {} GFM items (coarsen={}, resampling={})",
            len(items),
            self.coarsen_factor,
            self.resampling,
        )

        for idx, item in enumerate(items):
            xx = self._load_item(item, aoi, crs_src, resolution)
            if xx is None:
                continue

            # Build per-class 0/1 masks at native resolution, then mean-pool to
            # the coarsened grid. Mean-pooling a binary mask yields the fraction
            # of sub-pixels in each class — the correct way to downsample nominal
            # codes, and consistent with the average reproject that follows. A
            # categorical max would rank codes by number (and let nodata=255 win
            # every mixed block), which is meaningless for class labels.
            flood_mask_native, water_mask_native, valid_mask_native = self._build_native_masks(
                xx["ensemble_flood_extent"],
                xx["ensemble_water_extent"],
                xx["reference_water_mask"],
                self.coarsen_factor,
            )

            masks = xr.Dataset(
                {
                    "flood": flood_mask_native,
                    "water": water_mask_native,
                    "valid": valid_mask_native,
                }
            ).rio.write_crs(crs_src)

            code_bands = xr.Dataset(
                {
                    "reference_water": xx["reference_water_mask"].squeeze(drop=True),
                    "exclusion_mask": xx["exclusion_mask"].squeeze(drop=True),
                    "advisory_flags": xx["advisory_flags"].squeeze(drop=True),
                }
            ).rio.write_crs(crs_src)
            likelihood_band = xr.Dataset(
                {"ensemble_likelihood": xx["ensemble_likelihood"].squeeze(drop=True).astype("float32")}
            ).rio.write_crs(crs_src)

            # Reproject each mask directly onto the ~80 m global
            # grid (pre-computed snapped bounds/transform). This ensures all
            # items accumulate on the same aligned grid — no double
            # reprojection needed at harmonisation time.
            masks_ll = self._reproject_to_canonical_grid(masks)
            codes_ll = self._reproject_codes_to_canonical_grid(code_bands)
            likelihood_ll = self._reproject_likelihood_to_canonical_grid(likelihood_band)
            del xx, flood_mask_native, water_mask_native, valid_mask_native, masks, code_bands, likelihood_band

            flood_frac = np.squeeze(masks_ll["flood"].fillna(0.0).values.astype("float32"))
            water_frac = np.squeeze(masks_ll["water"].fillna(0.0).values.astype("float32"))
            valid_frac = np.squeeze(masks_ll["valid"].fillna(0.0).values.astype("float32"))
            ref_codes = np.squeeze(codes_ll["reference_water"].values).astype(np.uint8)
            excl_codes = np.squeeze(codes_ll["exclusion_mask"].values).astype(np.uint8)
            advisory = np.squeeze(codes_ll["advisory_flags"].values).astype(np.uint8)
            likelihood = np.squeeze(likelihood_ll["ensemble_likelihood"].values).astype(np.float32)

            # Initialize accumulators on first valid load
            if flood_count is None:
                shape = flood_frac.shape
                flood_count = np.zeros(shape, dtype=np.float32)
                water_count = np.zeros(shape, dtype=np.float32)
                valid_count = np.zeros(shape, dtype=np.float32)
                reference_water_codes = ref_codes.copy()
                exclusion_codes = excl_codes.copy()
                advisory_flags = advisory.copy()
                ensemble_likelihood = likelihood.copy()
                ref_coords = masks_ll["flood"].coords
                ref_dims = masks_ll["flood"].dims
            else:
                reference_water_codes = _masked_max(reference_water_codes, ref_codes, GFM_NODATA)
                exclusion_codes = _masked_max(exclusion_codes, excl_codes, GFM_NODATA)
                advisory_flags = _masked_or(advisory_flags, advisory, GFM_NODATA)
                ensemble_likelihood = np.fmax(ensemble_likelihood, likelihood)

            flood_count += flood_frac
            water_count += water_frac
            valid_count += valid_frac

            del (
                masks_ll,
                codes_ll,
                likelihood_ll,
                flood_frac,
                water_frac,
                valid_frac,
                ref_codes,
                excl_codes,
                advisory,
                likelihood,
            )
            logger.debug("Item {}/{} processed", idx + 1, len(items))

        if flood_count is None or valid_count is None:
            logger.warning("No valid data found in {} items", len(items))
            return None

        # Compute derived products
        extra_layers: dict[str, np.ndarray] = {
            "exclusion_mask": exclusion_codes,
            "advisory_flags": advisory_flags,
        }
        if ensemble_likelihood is not None:
            likelihood_codes = np.full(ensemble_likelihood.shape, GFM_NODATA, dtype=np.uint8)
            valid_likelihood = np.isfinite(ensemble_likelihood)
            likelihood_codes[valid_likelihood] = np.rint(
                np.clip(ensemble_likelihood[valid_likelihood], 0.0, 100.0)
            ).astype(np.uint8)
            extra_layers["ensemble_likelihood"] = likelihood_codes
        processed = self._classify(
            flood_count,
            water_count,
            valid_count,
            ref_coords,
            ref_dims,
            reference_water_codes=reference_water_codes,
            extra_layers=extra_layers,
        )

        # Build metadata
        metadata = TileMetadata(
            event_id=event_id,
            source_id="gfm",
            fetch_timestamp=datetime.now(timezone.utc),
            crs="EPSG:4326",
            resolution=self.reprojector.target_resolution,
            bbox=self._snapped_bounds,
            cloud_fraction=processed.cloud_fraction,
            permanent_water_mask_available=processed.reference_water is not None,
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
        import pyproj
        import rioxarray  # noqa: F401
        from shapely.geometry import box

        first_item = items[0]
        crs_src = pyproj.CRS.from_wkt(first_item.properties["proj:wkt2"])
        resolution = first_item.properties["gsd"]

        west, south, east, north = self.bbox
        aoi = box(west, south, east, north)

        # Accumulators: masked-max / OR of codes across items (nodata=255)
        efe_accum: np.ndarray | None = None
        rwm_accum: np.ndarray | None = None
        extra_accum: dict[str, np.ndarray] = {}

        logger.info(
            "Processing {} GFM items in native mode (nearest-neighbour reproject, no coarsen)",
            len(items),
        )

        for idx, item in enumerate(items):
            xx = self._load_item(item, aoi, crs_src, resolution)
            if xx is None:
                continue

            # Reproject raw codes to the ~80 m processed grid with NN;
            # codes are discrete so continuous resampling would corrupt them.
            codes_ds = xr.Dataset(
                {
                    "ensemble_flood_extent": xx["ensemble_flood_extent"].squeeze(drop=True),
                    "ensemble_water_extent": xx["ensemble_water_extent"].squeeze(drop=True),
                    "reference_water_mask": xx["reference_water_mask"].squeeze(drop=True),
                    "exclusion_mask": xx["exclusion_mask"].squeeze(drop=True),
                    "ensemble_likelihood": xx["ensemble_likelihood"].squeeze(drop=True),
                    "advisory_flags": xx["advisory_flags"].squeeze(drop=True),
                }
            ).rio.write_crs(crs_src)
            codes_ll = self._reproject_codes_to_canonical_grid(codes_ds)

            efe = np.squeeze(codes_ll["ensemble_flood_extent"].values).astype(np.uint8)
            rwm = np.squeeze(codes_ll["reference_water_mask"].values).astype(np.uint8)
            extra = {
                "ensemble_water_extent": np.squeeze(codes_ll["ensemble_water_extent"].values).astype(np.uint8),
                "exclusion_mask": np.squeeze(codes_ll["exclusion_mask"].values).astype(np.uint8),
                "ensemble_likelihood": np.squeeze(codes_ll["ensemble_likelihood"].values).astype(np.uint8),
                "advisory_flags": np.squeeze(codes_ll["advisory_flags"].values).astype(np.uint8),
            }
            del xx, codes_ds, codes_ll

            if efe_accum is None:
                efe_accum = efe.copy()
                rwm_accum = rwm.copy()
                extra_accum = {name: values.copy() for name, values in extra.items()}
            else:
                # Masked max: valid code beats nodata; max of two valid codes wins.
                efe_accum = _masked_max(efe_accum, efe, GFM_NODATA)
                rwm_accum = _masked_max(rwm_accum, rwm, GFM_NODATA)
                extra_accum["ensemble_water_extent"] = _masked_max(
                    extra_accum["ensemble_water_extent"],
                    extra["ensemble_water_extent"],
                    GFM_NODATA,
                )
                extra_accum["exclusion_mask"] = _masked_max(
                    extra_accum["exclusion_mask"],
                    extra["exclusion_mask"],
                    GFM_NODATA,
                )
                extra_accum["ensemble_likelihood"] = _masked_max(
                    extra_accum["ensemble_likelihood"],
                    extra["ensemble_likelihood"],
                    GFM_NODATA,
                )
                extra_accum["advisory_flags"] = _masked_or(
                    extra_accum["advisory_flags"],
                    extra["advisory_flags"],
                    GFM_NODATA,
                )

            logger.debug("Item {}/{} processed (native)", idx + 1, len(items))

        if efe_accum is None or rwm_accum is None:
            logger.warning("No valid data found in {} items", len(items))
            return None

        processed = self._build_native_tile(efe_accum, rwm_accum, extra_layers=extra_accum)

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
        water_native: "xr.DataArray",
        reference_native: "xr.DataArray",
        coarsen_factor: int = 1,
    ) -> tuple["xr.DataArray", "xr.DataArray", "xr.DataArray"]:
        """Build float32 flood, water, and validity coverage masks.

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
        water_mask = (water_native == GFM_WATER).astype("float32")
        # An observation contributes to "valid" if any core band has a non-nodata code.
        valid_mask = (
            (flood_native != GFM_NODATA) | (water_native != GFM_NODATA) | (reference_native != GFM_NODATA)
        ).astype("float32")
        if coarsen_factor > 1:
            flood_mask = flood_mask.coarsen(y=coarsen_factor, x=coarsen_factor, boundary="trim").mean()
            water_mask = water_mask.coarsen(y=coarsen_factor, x=coarsen_factor, boundary="trim").mean()
            valid_mask = valid_mask.coarsen(y=coarsen_factor, x=coarsen_factor, boundary="trim").mean()
        return flood_mask, water_mask, valid_mask

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

    def _reproject_likelihood_to_canonical_grid(self, likelihood: "xr.Dataset") -> "xr.Dataset":
        """Reproject native likelihood values to the canonical grid with averaging."""
        likelihood = likelihood.squeeze(drop=True)
        likelihood = likelihood.where(likelihood != GFM_NODATA, np.nan)
        return likelihood.rio.reproject(
            "EPSG:4326",
            nodata=np.nan,
            resampling=Resampling.average,
            shape=(self._dst_height, self._dst_width),
            transform=self._dst_transform,
        )

    def _build_native_tile(
        self,
        efe: np.ndarray,
        rwm: np.ndarray,
        *,
        extra_layers: dict[str, np.ndarray] | None = None,
    ) -> GfmProcessedTile:
        """Build a GfmProcessedTile holding native band arrays.

        Args:
            efe: ensemble_flood_extent uint8 array on the canonical grid.
            rwm: reference_water_mask uint8 array on the canonical grid.
            extra_layers: Optional dict of extra native-code arrays (e.g.
                ensemble_water_extent, exclusion_mask) keyed by layer name.

        Returns:
            GfmProcessedTile with native fields populated.
        """
        total_pixels = efe.size
        valid_pixels = int(np.sum(efe != GFM_NODATA))
        cloud_fraction = 1.0 - (valid_pixels / total_pixels) if total_pixels > 0 else 1.0
        return GfmProcessedTile(
            ensemble_flood_extent=efe,
            reference_water_mask=rwm,
            extra_layers=extra_layers or {},
            transform=self._dst_transform,
            crs="EPSG:4326",
            shape=efe.shape,
            cloud_fraction=cloud_fraction,
        )

    def _classify(
        self,
        flood_count: np.ndarray,
        water_count: np.ndarray,
        valid_count: np.ndarray,
        coords,
        dims,
        *,
        reference_water_codes: np.ndarray | None = None,
        extra_layers: dict[str, np.ndarray] | None = None,
    ) -> GfmProcessedTile:
        """Compute water/flood fractions plus code-preserving reference water.

        The counts are float accumulators of per-pixel class coverage fractions
        (one contribution per item, in ``[0, 1]``). The per-layer maths lives in
        the GFM layer registry (:mod:`atlantis.fetchers.gfm.derived`).
        """
        ctx = DerivationContext(
            arrays={
                ENSEMBLE_FLOOD_EXTENT_COUNT: flood_count,
                ENSEMBLE_WATER_EXTENT_COUNT: water_count,
                VALID_COUNT: valid_count,
                REFERENCE_WATER_MASK_CODES: (
                    reference_water_codes
                    if reference_water_codes is not None
                    else np.full(flood_count.shape, GFM_NODATA, dtype=np.uint8)
                ),
            }
        )
        water_fraction = registry.get_derived("water_fraction").derive(ctx)
        flood_fraction = registry.get_derived("flood_fraction").derive(ctx)
        reference_water = registry.get_derived("reference_water").derive(ctx)

        # Coverage fraction (proxy for cloud/missing)
        total_pixels = water_fraction.size
        valid_pixels = int(np.sum(valid_count > 0))
        cloud_fraction = 1.0 - (valid_pixels / total_pixels) if total_pixels > 0 else 1.0

        # Use the pre-computed canonical grid transform
        shape = water_fraction.shape
        return GfmProcessedTile(
            water_fraction=water_fraction,
            flood_fraction=flood_fraction,
            reference_water=reference_water,
            extra_layers=extra_layers or {},
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

        Writes classified layers (water_fraction, flood_fraction, reference_water)
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
            extra_paths: dict[str, Path] = {}
            for name, array in processed.extra_layers.items():
                spec = registry.get_native(name)
                extra_paths[name] = _write_tif(array, name, spec.dtype, spec.nodata)
            return GfmOutputPaths(
                ensemble_flood_extent=efe_path,
                reference_water_mask=rwm_path,
                extra=extra_paths,
            )

        # Classified mode — fractions as uint8 percent (0–100) nodata 255,
        # mirroring the VIIRS/MODIS convention.
        wf_src = np.squeeze(processed.water_fraction)
        wf_pct = np.full(wf_src.shape, GFM_NODATA, dtype=np.uint8)
        wf_valid = np.isfinite(wf_src)
        wf_pct[wf_valid] = np.round(np.clip(wf_src[wf_valid], 0.0, 1.0) * 100).astype(np.uint8)
        wf_path = _write_tif(wf_pct, "water_fraction", "uint8", GFM_NODATA)

        ff_src = np.squeeze(processed.flood_fraction)
        ff_pct = np.full(ff_src.shape, GFM_NODATA, dtype=np.uint8)
        ff_valid = np.isfinite(ff_src)
        ff_pct[ff_valid] = np.round(np.clip(ff_src[ff_valid], 0.0, 1.0) * 100).astype(np.uint8)
        ff_path = _write_tif(ff_pct, "flood_fraction", "uint8", GFM_NODATA)
        rw_path = _write_tif(processed.reference_water, "reference_water", "uint8", GFM_NODATA)

        extra_paths: dict[str, Path] = {}
        for name, array in processed.extra_layers.items():
            spec = registry.get_native(name)
            extra_paths[name] = _write_tif(array, name, spec.dtype, spec.nodata)

        return GfmOutputPaths(
            water_fraction=wf_path,
            flood_fraction=ff_path,
            reference_water=rw_path,
            extra=extra_paths,
        )

    @staticmethod
    def aggregate_tiles(tiles: list[GfmProcessedTile]) -> GfmProcessedTile | None:
        """Aggregate multiple date-group tiles into one (for aggregate strategy).

        Dispatches each layer to the shared :func:`~atlantis.layers.aggregate_layer`
        engine using the per-layer aggregation declared in the GFM registry.
        Classified mode averages fractions and reduces code bands with
        masked-max / masked-or; native mode applies the same code-band reductions
        to the raw bands.
        """
        if not tiles:
            return None
        if len(tiles) == 1:
            return tiles[0]

        ref = tiles[0]
        is_native = ref.ensemble_flood_extent is not None

        # Collect per-layer stacks from the appropriate tile fields.
        stacks: dict[str, list[np.ndarray]] = {}
        if is_native:
            stacks["ensemble_flood_extent"] = [t.ensemble_flood_extent for t in tiles]
            stacks["reference_water_mask"] = [t.reference_water_mask for t in tiles]
        else:
            stacks["water_fraction"] = [t.water_fraction for t in tiles]
            stacks["flood_fraction"] = [t.flood_fraction for t in tiles]
            stacks["reference_water"] = [t.reference_water for t in tiles]

        extra_names = {name for t in tiles for name in t.extra_layers}
        for name in extra_names:
            stacks[name] = [t.extra_layers.get(name) for t in tiles]

        # Build a usable-observation mask from the exclusion_mask stack when it
        # is present. Only the ``majority`` operator consumes it.
        valid_stack: np.ndarray | None = None
        if "exclusion_mask" in stacks:
            em_arrays = [a for a in stacks["exclusion_mask"] if a is not None]
            if em_arrays:
                em_stack = np.stack(em_arrays, axis=0)
                valid_stack = ~(em_stack > 0)

        # Reduce every layer through the shared engine, reading the operator and
        # nodata sentinel from the registry.
        reduced: dict[str, np.ndarray] = {}
        for name, arrays in stacks.items():
            present = [a for a in arrays if a is not None]
            if not present:
                continue
            spec = registry.get(name)
            op = spec.aggregation
            # ``majority`` needs a valid_stack whose time axis matches the layer
            # stack. Fall back to mode when they do not align.
            layer_valid_stack = None
            if op == "majority" and valid_stack is not None and valid_stack.shape[0] == len(present):
                layer_valid_stack = valid_stack
            elif op == "majority":
                op = "mode"
            reduced[name] = aggregate_layer(
                np.stack(present, axis=0),
                op,  # type: ignore[arg-type]
                nodata=spec.nodata,
                valid_stack=layer_valid_stack,
            )

        # Rebuild the source-specific tile type.
        if is_native:
            efe = reduced["ensemble_flood_extent"]
            total_pixels = efe.size
            valid_pixels = int(np.sum(efe != GFM_NODATA))
            cloud_fraction = 1.0 - (valid_pixels / total_pixels) if total_pixels > 0 else 1.0
            extra_layers = {name: reduced[name] for name in extra_names}
            return GfmProcessedTile(
                ensemble_flood_extent=efe,
                reference_water_mask=reduced["reference_water_mask"],
                extra_layers=extra_layers,
                transform=ref.transform,
                crs=ref.crs,
                shape=ref.shape,
                cloud_fraction=cloud_fraction,
            )

        water_fraction = reduced["water_fraction"]
        total_pixels = water_fraction.size
        valid_pixels = int(np.sum(np.isfinite(water_fraction)))
        cloud_fraction = 1.0 - (valid_pixels / total_pixels) if total_pixels > 0 else 1.0
        extra_layers = {name: reduced[name] for name in extra_names}
        return GfmProcessedTile(
            water_fraction=water_fraction,
            flood_fraction=reduced["flood_fraction"],
            reference_water=reduced["reference_water"],
            extra_layers=extra_layers,
            transform=ref.transform,
            crs=ref.crs,
            shape=ref.shape,
            cloud_fraction=cloud_fraction,
        )
