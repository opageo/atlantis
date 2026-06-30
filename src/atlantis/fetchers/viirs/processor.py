"""Raster processing for VIIRS flood data.

This module encapsulates the raster operations (mosaic, clip, classify, write)
that were previously mixed into the VIIRSFetcher.fetch() method.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Union

import numpy as np
import rasterio
from loguru import logger
from rasterio.io import MemoryFile
from rasterio.mask import mask
from rasterio.merge import merge
from shapely.geometry.base import BaseGeometry

# Code constants and the layer registry live in ``layers.py`` (the single source
# of truth). Re-exported here so existing
# ``from ...viirs.processor import FILL_CODES`` style imports keep working.
from atlantis.fetchers.viirs.layers import (  # noqa: F401 — re-exported for backwards compatibility
    CLASSIFIED_FLOOD_NODATA,
    CLOUD_CODES,
    FILL_CODES,
    FLOOD_MIN_CODE,
    OPEN_WATER_CODES,
    PERMANENT_WATER_CODES,
    SEASONAL_WATER_CODES,
    SELECTED_BAND,
    registry,
)
from atlantis.layers import DerivationContext
from atlantis.models.metadata import TileMetadata

if TYPE_CHECKING:
    from rasterio.io import DatasetReader


# Type alias: a tile location can be a local file path or a /vsicurl/ URL
TilePath = Union[Path, str]

_VSICURL_PREFIX = "/vsicurl/"

#: Derived layers carried as named ``ProcessedTile`` fields; everything else a
#: source registers goes through ``ProcessedTile.extra_layers``.
_CORE_DERIVED = ("flood_fraction", "quality_mask", "permanent_water")


def _decode_flood_fraction(data: np.ndarray) -> np.ndarray:
    """Decode raw VIIRS codes into flood fraction while preserving missing data.

    Thin wrapper delegating to the registered ``flood_fraction`` derivation so
    the decode lives in exactly one place
    (:mod:`atlantis.fetchers.viirs.derived`).
    """
    return registry.get_derived("flood_fraction").derive(DerivationContext(arrays={SELECTED_BAND: data}))


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
            Valid non-flood observations produce 0.0; fill and cloud pixels are
            preserved as NaN.
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
    #: Additional derived layers (e.g. cloud_mask, shadow) keyed by layer name.
    extra_layers: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def is_classified(self) -> bool:
        """True when derived layers are present rather than the raw band."""
        return self.raw is None


@dataclass(frozen=True)
class OutputPaths:
    """Paths for the output files."""

    raw: Path | None = None
    flood_fraction: Path | None = None
    quality_mask: Path | None = None
    permanent_water: Path | None = None
    #: Output paths for extra derived layers, keyed by layer name.
    extra: dict[str, Path] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessTilesResult:
    """Outcome of processing VIIRS tiles for one date."""

    paths: OutputPaths
    metadata: TileMetadata
    processed: ProcessedTile


def classify_viirs_pixels(data: np.ndarray, transform: rasterio.Affine, crs: str) -> "ProcessedTile":
    """Classify raw VIIRS pixel values into flood fraction and masks.

    Module-level so it is picklable by Dask workers without instantiating
    :class:`ViirsRasterProcessor`.

    Args:
        data: Raw pixel values (uint8) from a single-band VIIRS tile.
        transform: Affine transform of the array.
        crs: Coordinate reference system string (e.g. ``"EPSG:4326"``).

    Returns:
        :class:`ProcessedTile` with ``flood_fraction`` populated; other
        fields (``quality_mask``, ``permanent_water``) are set but callers
        that only need ``flood_fraction`` may discard them.
    """
    ctx = DerivationContext(arrays={SELECTED_BAND: data})
    derived = {spec.name: spec.derive(ctx) for spec in registry.list_derived()}
    extra = {name: arr for name, arr in derived.items() if name not in _CORE_DERIVED}

    fill = np.isin(data, list(FILL_CODES))
    cloud = np.isin(data, list(CLOUD_CODES))
    valid = ~fill
    n_valid = int(valid.sum())
    cloud_fraction = float(cloud[valid].sum() / n_valid) if n_valid else 0.0

    return ProcessedTile(
        flood_fraction=derived["flood_fraction"],
        quality_mask=derived["quality_mask"],
        permanent_water=derived["permanent_water"],
        extra_layers=extra,
        transform=transform,
        crs=crs,
        cloud_fraction=cloud_fraction,
    )


def classify_viirs_flood_fraction(data: np.ndarray) -> np.ndarray:
    """Decode raw VIIRS pixel codes into a continuous flood-fraction array.

    Lighter-weight sibling of :func:`classify_viirs_pixels` for batch
    workflows that only need ``flood_fraction``: skips the
    ``quality_mask`` / ``permanent_water_mask`` allocations entirely
    (~40 MB saved per 4448×4448 granule).

    VIIRS codes 101–200 encode water fraction as ``(code − 100) / 100``.
    Valid non-flood observations map to 0.0, while fill and cloud are preserved
    as NaN so downstream averaging skips them.

    Args:
        data: Raw pixel values (uint8) from a single-band VIIRS tile.

    Returns:
        ``float32`` array of flood fraction values in ``[0.0, 1.0]`` with
        NaN for fill/cloud pixels, same shape as *data*.
    """
    return _decode_flood_fraction(data)


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
                extra={name: output_dir / f"{base_name}_{name}.tif" for name in processed.extra_layers},
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

        logger.debug("Mosaicked {} tile(s) -> shape {}", len(tile_paths), mosaic.shape)

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

        logger.debug("Clipped to AOI -> shape {}", clipped.shape)

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
        """Classify pixel values — delegates to module-level :func:`classify_viirs_pixels`.

        The per-layer maths lives in the VIIRS layer registry
        (:mod:`atlantis.fetchers.viirs.derived`), so this is a thin wrapper that
        also emits a debug summary of the classification mix.
        """
        processed = classify_viirs_pixels(data, transform, crs)

        n_total = int(data.size)
        n_fill = int(np.isin(data, list(FILL_CODES)).sum())
        n_flood = int(((data >= 101) & (data <= 200)).sum())
        n_cloud = int(np.isin(data, list(CLOUD_CODES)).sum())
        n_perm_water = int(np.isin(data, list(PERMANENT_WATER_CODES)).sum())
        n_clear = n_total - n_fill - n_cloud - n_flood - n_perm_water
        if n_total:
            logger.debug(
                "Classification: flood {:.1f}%, cloud {:.1f}%, permanent-water"
                " {:.1f}%, clear {:.1f}%, fill/no-data {:.1f}%",
                n_flood / n_total * 100,
                n_cloud / n_total * 100,
                n_perm_water / n_total * 100,
                n_clear / n_total * 100,
                n_fill / n_total * 100,
            )
        return processed

    def write_processed(self, result: "ProcessTilesResult") -> None:
        """Persist a :class:`ProcessTilesResult` to disk.

        Convenience wrapper around :meth:`_write_outputs` so callers that receive
        a result from :meth:`process_tiles` (which may have been run with
        ``write_outputs=False``) can flush it to disk later — e.g. after a
        peak-window filter has been applied.

        The output directory must already exist; paths are taken from
        ``result.paths``.
        """
        self._write_outputs(result.processed, result.paths)

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
            # Store as uint8 percentage (0–100); missing flood_fraction values
            # (cloud/fill) round-trip as nodata=255.
            pct = np.full(processed.flood_fraction.shape, CLASSIFIED_FLOOD_NODATA, dtype=np.uint8)
            valid = ~np.isnan(processed.flood_fraction)
            pct[valid] = np.round(processed.flood_fraction[valid] * 100).clip(0, 100).astype(np.uint8)
            with rasterio.open(
                paths.flood_fraction,
                "w",
                **base_meta,
                dtype="uint8",
                nodata=CLASSIFIED_FLOOD_NODATA,
            ) as dst:
                dst.write(pct, 1)

        if paths.quality_mask is not None and processed.quality_mask is not None:
            with rasterio.open(paths.quality_mask, "w", **base_meta, dtype="uint8", nodata=0) as dst:
                dst.write(processed.quality_mask, 1)

        if paths.permanent_water is not None and processed.permanent_water is not None:
            with rasterio.open(paths.permanent_water, "w", **base_meta, dtype="uint8", nodata=0) as dst:
                dst.write(processed.permanent_water, 1)

        # Extra derived layers (e.g. cloud_mask, shadow): written generically
        # using each layer's registered dtype / nodata.
        for name, array in processed.extra_layers.items():
            path = paths.extra.get(name)
            if path is None:
                continue
            spec = registry.get_derived(name)
            nodata = 0 if spec.nodata is None else spec.nodata
            with rasterio.open(path, "w", **base_meta, dtype=spec.dtype, nodata=nodata) as dst:
                dst.write(array.astype(spec.dtype), 1)

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

    @staticmethod
    def aggregate_tiles(tiles: list[ProcessedTile]) -> ProcessedTile:
        """Aggregate multiple ``ProcessedTile`` instances across time.

        Uses mean for continuous variables (flood_fraction) and mode for
        categorical variables (quality_mask, permanent_water, raw).

        Args:
            tiles: Sequence of ``ProcessedTile`` instances sharing the same
                CRS and spatial footprint. At least one tile is required.

        Returns:
            A single ``ProcessedTile`` with aggregated arrays.

        Raises:
            ValueError: If *tiles* is empty.
        """
        if not tiles:
            raise ValueError("Cannot aggregate an empty list of tiles")

        if len(tiles) == 1:
            return tiles[0]

        ref = tiles[0]

        # ── flood_fraction: mean across time ────────────────────────────
        flood_arrays = [t.flood_fraction for t in tiles if t.flood_fraction is not None]
        flood_fraction: np.ndarray | None = None
        if flood_arrays:
            stack = np.stack(flood_arrays, axis=0)
            flood_fraction = np.nanmean(stack, axis=0).astype(np.float32)

        # ── quality_mask: any valid observation across time ─────────────
        quality_arrays = [t.quality_mask for t in tiles if t.quality_mask is not None]
        quality_mask: np.ndarray | None = None
        if quality_arrays:
            stack = np.stack(quality_arrays, axis=0).astype(np.uint8)
            quality_mask = np.any(stack > 0, axis=0).astype(np.uint8)

        # ── permanent_water: majority across valid observations only ────
        pw_arrays = [t.permanent_water for t in tiles if t.permanent_water is not None]
        permanent_water: np.ndarray | None = None
        if pw_arrays:
            stack = np.stack(pw_arrays, axis=0).astype(np.uint8)
            if quality_arrays and len(quality_arrays) == len(pw_arrays):
                valid_stack = np.stack(quality_arrays, axis=0).astype(bool)
                valid_count = valid_stack.sum(axis=0)
                pw_count = np.sum((stack > 0) & valid_stack, axis=0)
                permanent_water = np.where(valid_count > 0, (pw_count / valid_count) > 0.5, 0).astype(np.uint8)
            else:
                permanent_water = _mode_uint8(stack)

        # ── raw: mode across time ───────────────────────────────────────
        raw_arrays = [t.raw for t in tiles if t.raw is not None]
        raw: np.ndarray | None = None
        if raw_arrays:
            stack = np.stack(raw_arrays, axis=0).astype(np.uint8)
            raw = _mode_uint8(stack)

        # ── cloud_fraction: mean ────────────────────────────────────────
        cloud_fraction = float(np.mean([t.cloud_fraction for t in tiles]))

        # ── extra derived layers: per-pixel mode across time ────────────
        extra_layers: dict[str, np.ndarray] = {}
        extra_names = {name for t in tiles for name in t.extra_layers}
        for name in extra_names:
            arrays = [t.extra_layers[name] for t in tiles if name in t.extra_layers]
            stack = np.stack(arrays, axis=0).astype(np.uint8)
            extra_layers[name] = _mode_uint8(stack)

        return ProcessedTile(
            raw=raw,
            flood_fraction=flood_fraction,
            quality_mask=quality_mask,
            permanent_water=permanent_water,
            extra_layers=extra_layers,
            transform=ref.transform,
            crs=ref.crs,
            cloud_fraction=cloud_fraction,
        )


def _mode_uint8(stack: np.ndarray) -> np.ndarray:
    """Compute element-wise mode of a uint8 stack along axis 0.

    Uses bincount on flattened values per pixel position. Falls back to
    sorting-based mode if scipy is not available.

    Args:
        stack: 3D array ``(time, height, width)`` of uint8 values.

    Returns:
        2D array ``(height, width)`` with the most frequent value per pixel.
    """
    try:
        from scipy.stats import mode as scipy_mode

        result, _ = scipy_mode(stack, axis=0, keepdims=False)
        return result.astype(np.uint8)
    except ImportError:
        # Fallback: bincount approach — ~2× slower but no dependency required
        height, width = stack.shape[1], stack.shape[2]
        flat = stack.reshape(stack.shape[0], -1)
        modes = np.empty(flat.shape[1], dtype=np.uint8)
        for i in range(flat.shape[1]):
            counts = np.bincount(flat[:, i].astype(np.int16))
            modes[i] = counts.argmax()
        return modes.reshape(height, width)
