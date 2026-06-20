"""Raster processing for MODIS MCDWD flood data.

Mirrors :mod:`atlantis.fetchers.viirs.processor` but accommodates two
sensor-specific concerns:

1. **HDF4 extraction.** When a tile is delivered as a LAADS ``.hdf`` file,
   the per-composite ``Flood_*Day_250m`` subdataset must be opened from
   the HDF-EOS Grid block before mosaicing.
2. **Categorical pixel encoding.** MCDWD pixels are 0/1/2/3/255 — we
   binarise class 3 into ``flood_fraction`` (so the harmoniser's
   ``average`` resampling produces a fractional 1-arcmin output that is
   drop-in compatible with VIIRS) and surface ``recurring_flood``
   (class 2) as a MODIS-only mask layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Union

import numpy as np
import rasterio
from loguru import logger
from rasterio.io import MemoryFile
from rasterio.mask import mask
from rasterio.merge import merge
from rasterio.transform import from_bounds
from shapely.geometry.base import BaseGeometry

from atlantis.models.metadata import TileMetadata

if TYPE_CHECKING:
    from rasterio.io import DatasetReader


# Type alias: a tile location can be a local file path or a /vsicurl/ URL.
TilePath = Union[Path, str]

#: MCDWD pixel codes (Release 1.1, Dec 2025).
NO_WATER_CODE = 0
SURFACE_WATER_CODE = 1
RECURRING_FLOOD_CODE = 2
UNUSUAL_FLOOD_CODE = 3
INSUFFICIENT_DATA_CODE = 255

#: Native MCDWD raster size (one tile is 4800×4800 at 0.002083333° = 1/480°).
MODIS_TILE_PIXELS = 4800
MODIS_TILE_DEGREES = 10.0
MODIS_PIXEL_SIZE_DEG = MODIS_TILE_DEGREES / MODIS_TILE_PIXELS

#: Mapping of canonical composite names → MCDWD HDF4 subdataset suffixes.
COMPOSITE_TO_HDF_LAYER: dict[str, str] = {
    "F1": "Flood_1Day_250m",
    "F1C": "FloodCS_1Day_250m",
    "F2": "Flood_2Day_250m",
    "F3": "Flood_3Day_250m",
}

_VSICURL_PREFIX = "/vsicurl/"

# Filename token used to detect HDF4 inputs; subdatasets opened from inside
# an HDF4 file carry a ``HDF4_EOS:`` prefix.
_HDF4_SUFFIX = ".hdf"
_HDF4_SUBDATASET_PREFIX = "HDF4_EOS"


def _resolve_tile_path(item: TilePath) -> str:
    """Convert a tile location to a rasterio-compatible path string.

    - If it's a remote URL (``https://``), prepend ``/vsicurl/`` so GDAL
      streams it.
    - Otherwise return the bare local path.
    """
    if isinstance(item, str):
        if item.startswith(_HDF4_SUBDATASET_PREFIX):
            return item
        if item.startswith(("https://", "http://")):
            if not item.startswith(_VSICURL_PREFIX):
                return _VSICURL_PREFIX + item
            return item
        return item
    return str(item)


# ── Tile-grid helpers ────────────────────────────────────────────────────


def modis_tiles_for_bbox(bbox: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """Return the list of MODIS ``(h, v)`` tiles intersecting *bbox*.

    Implements the standard MODLAND linear lat/lon tile convention used by
    every MCDWD product:

        h = floor((lon + 180) / 10)
        v = floor((90 − lat) / 10)

    Args:
        bbox: ``(west, south, east, north)`` in EPSG:4326 (degrees).

    Returns:
        A sorted list of ``(h, v)`` tuples covering the bbox.

    Raises:
        ValueError: if ``south > north`` or the bbox crosses the antimeridian
            (``west > east``). Dateline-crossing AOIs are not supported in v1.
    """
    west, south, east, north = bbox
    if south > north:
        raise ValueError("MODIS bbox must be (west, south, east, north) with south <= north")
    if west > east:
        raise ValueError("MODIS bbox crosses the antimeridian (west > east); split it into two AOIs.")

    h_min = int(np.floor((west + 180.0) / 10.0))
    h_max = int(np.floor((east + 180.0) / 10.0))
    v_min = int(np.floor((90.0 - north) / 10.0))
    v_max = int(np.floor((90.0 - south) / 10.0))

    h_min = max(0, min(35, h_min))
    h_max = max(0, min(35, h_max))
    v_min = max(0, min(17, v_min))
    v_max = max(0, min(17, v_max))

    return sorted([(h, v) for h in range(h_min, h_max + 1) for v in range(v_min, v_max + 1)])


def tile_bounds_from_hv(h: int, v: int) -> tuple[float, float, float, float]:
    """Return ``(west, south, east, north)`` of the MODIS tile at ``(h, v)``."""
    if not 0 <= h <= 35:
        raise ValueError(f"h must be in [0, 35], got {h}")
    if not 0 <= v <= 17:
        raise ValueError(f"v must be in [0, 17], got {v}")
    west = -180.0 + h * MODIS_TILE_DEGREES
    east = west + MODIS_TILE_DEGREES
    north = 90.0 - v * MODIS_TILE_DEGREES
    south = north - MODIS_TILE_DEGREES
    return (west, south, east, north)


def parse_hv_from_filename(filename: str) -> tuple[int, int] | None:
    """Extract ``(h, v)`` from a MODIS filename (e.g. ``…h24v05…``)."""
    import re

    match = re.search(r"\.h(\d{2})v(\d{2})\.", filename)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


# ── Dataclasses ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProcessedTile:
    """Result from processing MCDWD tiles for a single date.

    Attributes:
        transform: Affine transform for the processed (clipped) raster.
        crs: Coordinate reference system (EPSG:4326).
        cloud_fraction: Fraction of pixels coded as ``255`` (insufficient
            data — includes cloud, terrain shadow, and HAND-masked terrain)
            within the AOI.
        raw: Original uint8 codes when ``--no-classify`` is used.
        flood_fraction: Binary float32 mask ``(class == 3).astype(float32)``.
            Drives the harmoniser ``average`` resampling to a true % flooded
            at coarser resolution.
        quality_mask: ``(class != 255).astype(uint8)`` — 1 = valid clear-sky
            observation, 0 = insufficient data or HAND-masked terrain.
        permanent_water: ``(class == 1).astype(uint8)``.
        recurring_flood: ``(class == 2).astype(uint8)`` — MODIS-only.
            Always ``None`` when ``--no-classify`` is used.
    """

    transform: rasterio.Affine
    crs: str
    cloud_fraction: float
    raw: np.ndarray | None = None
    flood_fraction: np.ndarray | None = None
    quality_mask: np.ndarray | None = None
    permanent_water: np.ndarray | None = None
    recurring_flood: np.ndarray | None = None


@dataclass(frozen=True)
class OutputPaths:
    """Paths for the output GeoTIFFs (``None`` means: don't write)."""

    raw: Path | None = None
    flood_fraction: Path | None = None
    quality_mask: Path | None = None
    permanent_water: Path | None = None
    recurring_flood: Path | None = None


@dataclass(frozen=True)
class ProcessTilesResult:
    """Outcome of processing MCDWD tiles for one date."""

    paths: OutputPaths
    metadata: TileMetadata
    processed: ProcessedTile


# ── HDF4 subdataset selection ────────────────────────────────────────────


def find_hdf4_subdataset(hdf_path: Path, composite: str) -> str:
    """Return the GDAL subdataset URI for the requested MCDWD composite.

    Uses GDAL directly (``osgeo.gdal``) because rasterio no longer supports
    opening HDF4 container files.

    Args:
        hdf_path: Path to a downloaded ``.hdf`` file.
        composite: One of ``F1``, ``F1C``, ``F2``, ``F3``.

    Returns:
        The fully-qualified ``HDF4_EOS:EOS_GRID:...`` URI for the matching
        flood subdataset.

    Raises:
        KeyError: if *composite* is not recognised.
        FileNotFoundError: if the HDF4 file has no matching subdataset
            (corrupt download or unexpected layer naming).
    """
    from osgeo import gdal  # type: ignore[import-not-found]

    layer_suffix = COMPOSITE_TO_HDF_LAYER[composite.upper()]

    ds = gdal.Open(str(hdf_path))
    if ds is None:
        raise FileNotFoundError(f"GDAL could not open HDF4 file: {hdf_path}")

    subdataset_info = ds.GetSubDatasets()  # list of (uri, description) tuples
    ds = None  # close

    subdatasets = [uri for uri, _desc in subdataset_info]
    for uri in subdatasets:
        # Subdataset URIs end with `…:Grid_Water_Composite:Flood_2Day_250m`
        # — match only on the suffix because path encoding varies.
        if uri.endswith(":" + layer_suffix):
            logger.debug("HDF4 subdataset for {} -> {}", composite, uri)
            return uri
    raise FileNotFoundError(
        f"Composite '{composite}' (subdataset '{layer_suffix}') not found in {hdf_path}. "
        f"Available subdatasets: {subdatasets}"
    )


def open_hdf4_tile(hdf_path: Path, composite: str) -> "DatasetReader":
    """Open the requested composite from an MCDWD HDF4 file.

    Uses GDAL directly to read the HDF4 subdataset data and geotransform,
    then wraps the result in a rasterio MemoryFile-backed dataset for
    downstream compatibility with :func:`rasterio.merge.merge`.

    Falls back to assigning the affine from ``tile_bounds_from_hv`` when
    GDAL fails to parse the HDF-EOS Grid metadata.
    """
    from osgeo import gdal, osr  # type: ignore[import-not-found]

    uri = find_hdf4_subdataset(hdf_path, composite)

    gdal_ds = gdal.Open(uri)
    if gdal_ds is None:
        raise RuntimeError(f"GDAL could not open subdataset: {uri}")

    band = gdal_ds.GetRasterBand(1)
    data = band.ReadAsArray()

    # Try to extract geotransform and CRS from GDAL metadata.
    gt = gdal_ds.GetGeoTransform()
    srs = gdal_ds.GetProjection()
    gdal_ds = None  # close

    # Determine if the geotransform is valid (not the default identity).
    has_valid_gt = gt is not None and gt != (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    has_valid_crs = bool(srs)

    if has_valid_gt and has_valid_crs:
        # GDAL geotransform: (origin_x, pixel_width, 0, origin_y, 0, pixel_height)
        transform = rasterio.Affine(gt[1], gt[2], gt[0], gt[4], gt[5], gt[3])
        # Convert OGC WKT to a CRS string rasterio understands.
        sr = osr.SpatialReference()
        sr.ImportFromWkt(srs)
        sr.AutoIdentifyEPSG()
        epsg = sr.GetAuthorityCode(None)
        crs = f"EPSG:{epsg}" if epsg else "EPSG:4326"
    else:
        logger.warning(
            "GDAL geotransform/CRS not found in HDF4 subdataset; falling back to filename parsing for {}", hdf_path
        )
        # Fallback: synthesise the affine from the tile h/v in the filename.
        hv = parse_hv_from_filename(hdf_path.name)
        if hv is None:
            raise RuntimeError(f"HDF4 file {hdf_path.name} lacks a geotransform and the filename has no h/v token")
        h, v = hv
        west, south, east, north = tile_bounds_from_hv(h, v)
        transform = from_bounds(west, south, east, north, MODIS_TILE_PIXELS, MODIS_TILE_PIXELS)
        crs = "EPSG:4326"

    # Wrap in a rasterio MemoryFile so downstream merge/mask works unchanged.
    logger.info("")
    meta = {
        "driver": "GTiff",
        "height": data.shape[0],
        "width": data.shape[1],
        "count": 1,
        "dtype": "uint8",
        "crs": crs,
        "transform": transform,
        "nodata": INSUFFICIENT_DATA_CODE,
    }
    memfile = MemoryFile()
    dst = memfile.open(**meta)
    dst.write(data.astype(np.uint8), 1)
    return dst


# ── Processor ────────────────────────────────────────────────────────────


class ModisRasterProcessor:
    """Processor for MCDWD raster operations.

    Pipeline:

    1. Resolve every input tile location (HDF4 subdataset URI for LAADS,
       local path or ``/vsicurl/`` URL for LANCE GeoTIFFs).
    2. Mosaic with :func:`rasterio.merge.merge`.
    3. Clip to the AOI via :func:`rasterio.mask.mask`.
    4. Classify the categorical 0/1/2/3/255 codes into VIIRS-parity layers.
    5. Optionally write a per-layer GeoTIFF set under ``processed/``.
    """

    def __init__(
        self,
        area_geometry: BaseGeometry,
        crs: str = "EPSG:4326",
        classify: bool = False,
        composite: str = "F2",
    ) -> None:
        """Initialise the processor with AOI clip geometry + composite choice."""
        self.area_geometry = area_geometry
        self.crs = crs
        self.classify = classify
        self.composite = composite.upper()
        if self.composite not in COMPOSITE_TO_HDF_LAYER:
            raise ValueError(f"Unknown composite '{composite}'. Expected one of: {list(COMPOSITE_TO_HDF_LAYER)}")

    def process_tiles(
        self,
        tile_paths: list[TilePath],
        event_id: str,
        date_token: str,
        output_dir: Path,
        *,
        write_outputs: bool = True,
    ) -> ProcessTilesResult | None:
        """Process a group of tiles for a single date."""
        if not tile_paths:
            return None

        processed = self._mosaic_and_clip(tile_paths)
        if processed is None:
            return None

        base_name = f"{event_id}_{date_token}_modis"
        if self.classify:
            paths = OutputPaths(
                flood_fraction=output_dir / f"{base_name}_flood_fraction.tif",
                quality_mask=output_dir / f"{base_name}_quality_mask.tif",
                permanent_water=output_dir / f"{base_name}_permanent_water.tif",
                recurring_flood=output_dir / f"{base_name}_recurring_flood.tif",
            )
        else:
            paths = OutputPaths(raw=output_dir / f"{base_name}_raw.tif")

        if write_outputs:
            self._write_outputs(processed, paths)

        ref_array = processed.raw if processed.raw is not None else processed.flood_fraction
        height, width = ref_array.shape  # type: ignore[union-attr]
        metadata = self._build_metadata(event_id, processed, width=width, height=height)
        return ProcessTilesResult(paths=paths, metadata=metadata, processed=processed)

    # ── Mosaic + clip ────────────────────────────────────────────────────

    def _resolve_to_geotiff_source(self, item: TilePath) -> str | "DatasetReader":
        """Resolve one input to a rasterio source.

        - Local ``.hdf`` paths → open the requested composite subdataset
          (returns a ``DatasetReader``).
        - Other paths/URLs → return the resolved ``/vsicurl/``-prefixed string
          for :func:`rasterio.open`.
        """
        if isinstance(item, Path) and item.suffix.lower() == _HDF4_SUFFIX:
            return open_hdf4_tile(item, self.composite)
        if isinstance(item, str) and item.lower().endswith(_HDF4_SUFFIX):
            return open_hdf4_tile(Path(item), self.composite)
        return _resolve_tile_path(item)

    def _mosaic_and_clip(self, tile_paths: list[TilePath]) -> ProcessedTile | None:
        srcs: list["DatasetReader"] = []
        opened_explicitly: list["DatasetReader"] = []
        try:
            for item in tile_paths:
                resolved = self._resolve_to_geotiff_source(item)
                if isinstance(resolved, str):
                    src = rasterio.open(resolved)
                else:
                    src = resolved
                    opened_explicitly.append(src)
                srcs.append(src)

            mosaic, transform = merge(srcs)
            meta = srcs[0].meta.copy()
        finally:
            for src in srcs:
                src.close()

        logger.debug("Mosaicked {} tile(s) -> shape {}", len(tile_paths), mosaic.shape)

        meta.update(
            {
                "height": mosaic.shape[1],
                "width": mosaic.shape[2],
                "transform": transform,
            }
        )
        # Force EPSG:4326 in case the HDF4 driver returned something weird.
        meta["crs"] = self.crs
        meta["dtype"] = "uint8"
        meta["nodata"] = INSUFFICIENT_DATA_CODE

        with MemoryFile() as memory_file:
            with memory_file.open(**meta) as ds:
                ds.write(mosaic)
                clipped, clipped_transform = mask(
                    ds,
                    [self.area_geometry],
                    crop=True,
                    filled=True,
                    nodata=INSUFFICIENT_DATA_CODE,
                )

        logger.debug("Clipped to AOI -> shape {}", clipped.shape)

        crs_value = meta.get("crs", self.crs)
        if hasattr(crs_value, "to_string"):
            crs_value = crs_value.to_string()
        elif crs_value is None:
            crs_value = self.crs

        if self.classify:
            return self._classify_pixels(clipped[0], clipped_transform, str(crs_value))
        # No-classify: keep raw codes.
        valid = clipped[0] != INSUFFICIENT_DATA_CODE
        cloud_fraction = float(1.0 - valid.sum() / valid.size) if valid.size else 0.0
        return ProcessedTile(
            raw=clipped[0],
            transform=clipped_transform,
            crs=str(crs_value),
            cloud_fraction=cloud_fraction,
        )

    def _classify_pixels(self, data: np.ndarray, transform: rasterio.Affine, crs: str) -> ProcessedTile:
        """Decode MCDWD codes 0/1/2/3/255 into VIIRS-parity layers."""
        flood_mask = data == UNUSUAL_FLOOD_CODE
        recurring_flood = (data == RECURRING_FLOOD_CODE).astype(np.uint8)
        permanent_water = (data == SURFACE_WATER_CODE).astype(np.uint8)
        # quality_mask: 1 = valid observation, 0 = insufficient data or HAND-masked.
        # Note: HAND-masked terrain reports as 255 — preserve that distinction.
        quality_mask = (data != INSUFFICIENT_DATA_CODE).astype(np.uint8)

        flood_fraction = flood_mask.astype(np.float32)

        valid = data != INSUFFICIENT_DATA_CODE
        cloud_fraction = float(1.0 - valid.sum() / valid.size) if valid.size else 0.0

        logger.debug(
            "Classification: {} flood, {} recurring, {} permanent-water, {} insufficient (cloud-like fraction {:.1f}%)",
            int(flood_mask.sum()),
            int(recurring_flood.sum()),
            int(permanent_water.sum()),
            int((~valid).sum()),
            cloud_fraction * 100,
        )
        return ProcessedTile(
            flood_fraction=flood_fraction,
            quality_mask=quality_mask,
            permanent_water=permanent_water,
            recurring_flood=recurring_flood,
            transform=transform,
            crs=crs,
            cloud_fraction=cloud_fraction,
        )

    # ── Output writing ──────────────────────────────────────────────────

    def _write_outputs(self, processed: ProcessedTile, paths: OutputPaths) -> None:
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
            with rasterio.open(paths.raw, "w", **base_meta, dtype="uint8", nodata=INSUFFICIENT_DATA_CODE) as dst:
                dst.write(processed.raw, 1)

        if paths.flood_fraction is not None and processed.flood_fraction is not None:
            # Store as uint8 percent (0–100) with nodata=255 to mirror VIIRS.
            pct = np.round(processed.flood_fraction * 100).astype(np.uint8)
            with rasterio.open(paths.flood_fraction, "w", **base_meta, dtype="uint8", nodata=255) as dst:
                dst.write(pct, 1)

        if paths.quality_mask is not None and processed.quality_mask is not None:
            with rasterio.open(paths.quality_mask, "w", **base_meta, dtype="uint8", nodata=0) as dst:
                dst.write(processed.quality_mask, 1)

        if paths.permanent_water is not None and processed.permanent_water is not None:
            with rasterio.open(paths.permanent_water, "w", **base_meta, dtype="uint8", nodata=0) as dst:
                dst.write(processed.permanent_water, 1)

        if paths.recurring_flood is not None and processed.recurring_flood is not None:
            with rasterio.open(paths.recurring_flood, "w", **base_meta, dtype="uint8", nodata=0) as dst:
                dst.write(processed.recurring_flood, 1)

    def _build_metadata(self, event_id: str, processed: ProcessedTile, width: int, height: int) -> TileMetadata:
        west = processed.transform.c
        east = processed.transform.c + processed.transform.a * width
        north = processed.transform.f
        south = processed.transform.f + processed.transform.e * height
        return TileMetadata(
            event_id=event_id,
            source_id="modis",
            fetch_timestamp=datetime.now(timezone.utc),
            crs=processed.crs,
            resolution=abs(processed.transform.a),
            bbox=(min(west, east), min(south, north), max(west, east), max(south, north)),
            cloud_fraction=processed.cloud_fraction,
            quality_bitmask=0,
            permanent_water_mask_available=processed.permanent_water is not None,
        )

    # ── Aggregation ─────────────────────────────────────────────────────

    @staticmethod
    def aggregate_tiles(tiles: list[ProcessedTile]) -> ProcessedTile:
        """Aggregate ``ProcessedTile`` instances across time.

        - ``flood_fraction``: nan-mean across the time axis.
        - ``recurring_flood`` / ``permanent_water`` / ``quality_mask`` / ``raw``: per-pixel mode.
        - ``cloud_fraction``: arithmetic mean of per-tile values.
        """
        if not tiles:
            raise ValueError("Cannot aggregate an empty list of tiles")
        if len(tiles) == 1:
            return tiles[0]

        ref = tiles[0]

        flood_arrays = [t.flood_fraction for t in tiles if t.flood_fraction is not None]
        flood_fraction = np.nanmean(np.stack(flood_arrays, axis=0), axis=0).astype(np.float32) if flood_arrays else None

        def _mode_layer(attr: str) -> np.ndarray | None:
            arrays = [getattr(t, attr) for t in tiles if getattr(t, attr) is not None]
            if not arrays:
                return None
            stack = np.stack(arrays, axis=0).astype(np.uint8)
            return _mode_uint8(stack)

        return ProcessedTile(
            raw=_mode_layer("raw"),
            flood_fraction=flood_fraction,
            quality_mask=_mode_layer("quality_mask"),
            permanent_water=_mode_layer("permanent_water"),
            recurring_flood=_mode_layer("recurring_flood"),
            transform=ref.transform,
            crs=ref.crs,
            cloud_fraction=float(np.mean([t.cloud_fraction for t in tiles])),
        )


def _mode_uint8(stack: np.ndarray) -> np.ndarray:
    """Compute element-wise mode of a uint8 stack along axis 0.

    Accepts both ``(time, height, width)`` and ``(time, *)`` arrays.
    """
    try:
        from scipy.stats import mode as scipy_mode

        result, _ = scipy_mode(stack, axis=0, keepdims=False)
        return result.astype(np.uint8)
    except ImportError:
        trailing_shape = stack.shape[1:]
        flat = stack.reshape(stack.shape[0], -1)
        modes = np.empty(flat.shape[1], dtype=np.uint8)
        for i in range(flat.shape[1]):
            counts = np.bincount(flat[:, i].astype(np.int16))
            modes[i] = counts.argmax()
        return modes.reshape(trailing_shape)
