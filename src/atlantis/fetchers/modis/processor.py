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

# Pixel-code constants and the layer registry live in ``layers.py`` (the single
# source of truth). They are re-exported here so existing
# ``from ...modis.processor import INSUFFICIENT_DATA_CODE`` imports keep working.
from atlantis.fetchers.modis.layers import (  # noqa: F401 — re-exported for backwards compatibility
    INSUFFICIENT_DATA_CODE,
    NO_WATER_CODE,
    RECURRING_FLOOD_CODE,
    SELECTED_COMPOSITE,
    SURFACE_WATER_CODE,
    UNUSUAL_FLOOD_CODE,
    registry,
)
from atlantis.layers import DerivationContext
from atlantis.models.metadata import TileMetadata

if TYPE_CHECKING:
    from rasterio.io import DatasetReader


# Type alias: a tile location can be a local file path or a /vsicurl/ URL.
TilePath = Union[Path, str]

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
        cloud_fraction: Fraction of pixels coded as ``255`` ("Insufficient
            data", per NASA MCDWD User Guide Rev F Table 7) within the AOI.
            The field name is historical and a misnomer: code ``255`` is a
            catch-all sentinel and what feeds into it depends on the composite.
            The embedded LANCE TIFF ``description`` tag is authoritative for a
            given file; e.g. F2 says ``no clouds removed; terrain shadow pixels
            masked; HAND pixels masked``, while F1C also removes cloud shadow.
            Treat this value as "fraction of insufficient-data pixels", not
            literal cloud cover.
        raw: Original uint8 codes when ``--no-classify`` is used.
        water_fraction: Binary float32 mask covering classes 1/2/3.
            Pixels coded as ``255`` are stored as ``NaN`` so downstream
            averaging ignores insufficient data instead of treating it as dry
            land.
        flood_fraction: Binary float32 mask ``(class == 3).astype(float32)``.
            Pixels coded as ``255`` are stored as ``NaN`` so downstream
            averaging ignores insufficient data instead of treating it as dry
            land.
        exclusion_mask: ``(class == 255).astype(uint8)`` — 1 = excluded or
            invalid, 0 = usable classification.
        reference_water: ``isin(class, [1, 2]).astype(uint8)``.
        recurring_flood: ``(class == 2).astype(uint8)`` — MODIS-only.
            Always ``None`` when ``--no-classify`` is used.
    """

    transform: rasterio.Affine
    crs: str
    cloud_fraction: float
    raw: np.ndarray | None = None
    water_fraction: np.ndarray | None = None
    flood_fraction: np.ndarray | None = None
    exclusion_mask: np.ndarray | None = None
    reference_water: np.ndarray | None = None
    recurring_flood: np.ndarray | None = None

    @property
    def is_classified(self) -> bool:
        """True when derived layers are present rather than the raw band."""
        return self.raw is None


@dataclass(frozen=True)
class OutputPaths:
    """Paths for the output GeoTIFFs (``None`` means: don't write)."""

    raw: Path | None = None
    water_fraction: Path | None = None
    flood_fraction: Path | None = None
    exclusion_mask: Path | None = None
    reference_water: Path | None = None
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

    Uses ``osgeo.gdal`` directly because rasterio bundles its own GDAL build
    which may lack HDF4 support (e.g. the PyPI wheel).

    Args:
        hdf_path: Path to a downloaded ``.hdf`` file.
        composite: One of ``F1``, ``F1C``, ``F2``, ``F3``.

    Returns:
        The fully-qualified ``HDF4_EOS:EOS_GRID:...`` URI for the matching
        flood subdataset.

    Raises:
        KeyError: if *composite* is not recognised.
        RuntimeError: if osgeo.gdal is not available.
        FileNotFoundError: if the HDF4 file has no matching subdataset
            (corrupt download or unexpected layer naming).
    """
    try:
        from osgeo import gdal as _gdal  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "osgeo.gdal is required to list HDF4 subdatasets. "
            "Install GDAL with HDF4 support and reinstall the GDAL Python bindings."
        ) from exc

    if _gdal.GetDriverByName("HDF4") is None:
        raise RuntimeError("GDAL was compiled without HDF4 support; cannot read MCDWD .hdf files")

    layer_suffix = COMPOSITE_TO_HDF_LAYER[composite.upper()]
    ds = _gdal.Open(str(hdf_path))
    if ds is None:
        raise FileNotFoundError(
            f"GDAL could not open {hdf_path}. Is it a valid HDF4 file? Ensure the authentication layer "
            f"did not respond with an HTML page instead of the binary data."
        )
    subdatasets = ds.GetSubDatasets()  # list of (uri, description) tuples
    ds.Close()  # close
    uris = [uri for uri, _desc in subdatasets]
    for uri in uris:
        if uri.endswith(":" + layer_suffix):
            logger.debug("HDF4 subdataset for {} -> {}", composite, uri)
            return uri
    raise FileNotFoundError(
        f"Composite '{composite}' (subdataset '{layer_suffix}') not found in {hdf_path}. Available subdatasets: {uris}"
    )


def open_hdf4_tile(hdf_path: Path, composite: str) -> "DatasetReader":
    """Open the requested composite from an MCDWD HDF4 file.

    Uses ``osgeo.gdal`` to translate the HDF4 subdataset to an in-memory
    GeoTIFF, then hands it to rasterio — this works even when rasterio's
    bundled GDAL lacks HDF4 support (e.g. the PyPI wheel).

    Falls back to assigning the affine from ``tile_bounds_from_hv`` when
    GDAL fails to parse the HDF-EOS Grid metadata.
    """
    try:
        from osgeo import gdal as _gdal  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "osgeo.gdal is required to read HDF4 tiles. "
            "Install GDAL with HDF4 support and reinstall the GDAL Python bindings."
        ) from exc

    uri = find_hdf4_subdataset(hdf_path, composite)

    # Translate the subdataset to an in-memory GeoTIFF so rasterio can open
    # it without needing HDF4 in its own bundled GDAL.
    vsimem_path = f"/vsimem/{hdf_path.stem}_{composite}.tif"
    translated = _gdal.Translate(vsimem_path, uri)
    if translated is None:
        raise RuntimeError(f"gdal.Translate failed for subdataset {uri}")
    translated.FlushCache()
    translated = None  # close before reading bytes

    vsifile = _gdal.VSIFOpenL(vsimem_path, "rb")
    _gdal.VSIFSeekL(vsifile, 0, 2)
    size = _gdal.VSIFTellL(vsifile)
    _gdal.VSIFSeekL(vsifile, 0, 0)
    data_bytes = _gdal.VSIFReadL(1, size, vsifile)
    _gdal.VSIFCloseL(vsifile)
    _gdal.Unlink(vsimem_path)

    # ``VSIFReadL`` returns a ``bytearray`` in the current GDAL bindings, but
    # rasterio's ``MemoryFile`` only accepts ``bytes`` (or a file-like). Cast
    # once here so callers always get a working dataset.
    if not isinstance(data_bytes, bytes):
        data_bytes = bytes(data_bytes)

    memfile = MemoryFile(data_bytes)
    src = memfile.open()
    # The DatasetReader is backed by the MemoryFile buffer; without a strong
    # reference the MemoryFile is GC'd as soon as this function returns and
    # subsequent reads (e.g. inside ``merge``) fail with
    # ``TIFFReadEncodedStrip() failed``. Pin it to the dataset so it lives
    # exactly as long as the caller's reader.
    src._atlantis_memfile = memfile  # type: ignore[attr-defined]

    needs_affine = src.transform == rasterio.Affine.identity() or not src.crs
    if not needs_affine:
        return src

    # Defensive fallback: synthesise the affine from the tile h/v.
    src.close()
    hv = parse_hv_from_filename(hdf_path.name)
    if hv is None:
        raise RuntimeError(f"HDF4 file {hdf_path.name} lacks an affine and the filename has no h/v token")
    h, v = hv
    west, south, east, north = tile_bounds_from_hv(h, v)
    transform = from_bounds(west, south, east, north, MODIS_TILE_PIXELS, MODIS_TILE_PIXELS)
    fallback_src = memfile.open()
    data = fallback_src.read(1)
    fallback_src.close()
    memfile.close()

    out_memfile = MemoryFile()
    dst = out_memfile.open(
        driver="GTiff",
        height=MODIS_TILE_PIXELS,
        width=MODIS_TILE_PIXELS,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
        nodata=INSUFFICIENT_DATA_CODE,
    )
    dst.write(data, 1)
    # Pin the synthesised MemoryFile so the returned dataset stays readable.
    dst._atlantis_memfile = out_memfile  # type: ignore[attr-defined]
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
                water_fraction=output_dir / f"{base_name}_water_fraction.tif",
                flood_fraction=output_dir / f"{base_name}_flood_fraction.tif",
                exclusion_mask=output_dir / f"{base_name}_exclusion_mask.tif",
                reference_water=output_dir / f"{base_name}_reference_water.tif",
                recurring_flood=output_dir / f"{base_name}_recurring_flood.tif",
            )
        else:
            paths = OutputPaths(raw=output_dir / f"{base_name}_raw.tif")

        if write_outputs:
            self._write_outputs(processed, paths)

        ref_array = processed.raw if processed.raw is not None else processed.water_fraction
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
        """Decode MCDWD codes 0/1/2/3/255 into VIIRS-parity layers.

        Delegates the per-layer maths to the MODIS layer registry so the
        derivation logic lives in one declarative place
        (:mod:`atlantis.fetchers.modis.derived`).
        """
        ctx = DerivationContext(arrays={SELECTED_COMPOSITE: data})
        valid = data != INSUFFICIENT_DATA_CODE
        cloud_fraction = float(1.0 - valid.sum() / valid.size) if valid.size else 0.0

        water_fraction = registry.get_derived("water_fraction").derive(ctx)
        flood_fraction = registry.get_derived("flood_fraction").derive(ctx)
        exclusion_mask = registry.get_derived("exclusion_mask").derive(ctx)
        reference_water = registry.get_derived("reference_water").derive(ctx)
        recurring_flood = registry.get_derived("recurring_flood").derive(ctx)

        logger.debug(
            "Classification: {} water, {} flood, {} recurring,"
            " {} reference-water, {} insufficient (255 fraction {:.1f}%)",
            int(np.nansum(water_fraction)),
            int(np.nansum(flood_fraction)),
            int(recurring_flood.sum()),
            int(reference_water.sum()),
            int((~valid).sum()),
            cloud_fraction * 100,
        )
        return ProcessedTile(
            water_fraction=water_fraction,
            flood_fraction=flood_fraction,
            exclusion_mask=exclusion_mask,
            reference_water=reference_water,
            recurring_flood=recurring_flood,
            transform=transform,
            crs=crs,
            cloud_fraction=cloud_fraction,
        )

    # ── Output writing ──────────────────────────────────────────────────

    def write_processed(self, processed: ProcessedTile, paths: OutputPaths) -> None:
        """Write processed-tile layers to ``paths`` (public wrapper for callers).

        Used by the fetcher to defer writing processed/ GeoTIFFs until after
        peak-window filtering, so only surviving dates are persisted.
        """
        self._write_outputs(processed, paths)

    def _write_outputs(self, processed: ProcessedTile, paths: OutputPaths) -> None:
        ref_shape = processed.raw.shape if processed.raw is not None else processed.water_fraction.shape
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

        if paths.water_fraction is not None and processed.water_fraction is not None:
            pct = np.full(processed.water_fraction.shape, INSUFFICIENT_DATA_CODE, dtype=np.uint8)
            valid = np.isfinite(processed.water_fraction)
            pct[valid] = np.round(np.clip(processed.water_fraction[valid], 0.0, 1.0) * 100).astype(np.uint8)
            with rasterio.open(paths.water_fraction, "w", **base_meta, dtype="uint8", nodata=255) as dst:
                dst.write(pct, 1)

        if paths.flood_fraction is not None and processed.flood_fraction is not None:
            # Store as uint8 percent (0–100) with nodata=255 to mirror VIIRS.
            pct = np.full(processed.flood_fraction.shape, INSUFFICIENT_DATA_CODE, dtype=np.uint8)
            valid = np.isfinite(processed.flood_fraction)
            pct[valid] = np.round(np.clip(processed.flood_fraction[valid], 0.0, 1.0) * 100).astype(np.uint8)
            with rasterio.open(paths.flood_fraction, "w", **base_meta, dtype="uint8", nodata=255) as dst:
                dst.write(pct, 1)

        if paths.exclusion_mask is not None and processed.exclusion_mask is not None:
            with rasterio.open(paths.exclusion_mask, "w", **base_meta, dtype="uint8", nodata=255) as dst:
                dst.write(processed.exclusion_mask, 1)

        if paths.reference_water is not None and processed.reference_water is not None:
            with rasterio.open(paths.reference_water, "w", **base_meta, dtype="uint8", nodata=255) as dst:
                dst.write(processed.reference_water, 1)

        if paths.recurring_flood is not None and processed.recurring_flood is not None:
            with rasterio.open(paths.recurring_flood, "w", **base_meta, dtype="uint8", nodata=255) as dst:
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
            permanent_water_mask_available=processed.reference_water is not None,
        )

    # ── Aggregation ─────────────────────────────────────────────────────

    @staticmethod
    def aggregate_tiles(tiles: list[ProcessedTile]) -> ProcessedTile:
        """Aggregate ``ProcessedTile`` instances across time.

        - ``water_fraction`` / ``flood_fraction``: nan-mean across the time axis.
        - ``recurring_flood`` / ``reference_water`` / ``exclusion_mask`` / ``raw``: per-pixel mode.
        - ``cloud_fraction``: arithmetic mean of per-tile values.
        """
        if not tiles:
            raise ValueError("Cannot aggregate an empty list of tiles")
        if len(tiles) == 1:
            return tiles[0]

        ref = tiles[0]

        water_arrays = [t.water_fraction for t in tiles if t.water_fraction is not None]
        water_fraction = np.nanmean(np.stack(water_arrays, axis=0), axis=0).astype(np.float32) if water_arrays else None

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
            water_fraction=water_fraction,
            flood_fraction=flood_fraction,
            exclusion_mask=_mode_layer("exclusion_mask"),
            reference_water=_mode_layer("reference_water"),
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
