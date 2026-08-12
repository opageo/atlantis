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
from dataclasses import dataclass, field, replace
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

# Measured: a single eager odc.stac.load of all 6 GFM_BANDS at native ~20 m
# resolution over a full EQUI7 tile peaks ~15 GiB RSS. Splitting the load
# keeps at most 3 native bands + derivatives resident instead of 6 + 3.

#: Feeds `_build_native_masks` (flood/water/valid) and `reference_water`.
_CLASSIFIED_MASK_BANDS: list[str] = ["ensemble_flood_extent", "ensemble_water_extent", "reference_water_mask"]

#: Not needed until after the first group's native buffers are freed.
_CLASSIFIED_CODE_BANDS: list[str] = ["exclusion_mask", "advisory_flags", "ensemble_likelihood"]

# ── Windowed native processing ────────────────────────────────────────────
#
# Tiles the native grid into pixel-aligned windows, running load → mask →
# coarsen → reproject once per window, accumulated like partial-coverage
# items. Window size must be an exact multiple of coarsen_factor so
# `coarsen(..., boundary="trim")` never trims inside a window.


def _squeeze_time(obj: "_T") -> "_T":
    """Drop a singleton ``time`` dimension only — never a spatial one.

    Plain ``.squeeze(drop=True)`` drops *every* size-1 dimension. That's
    harmless for the unwindowed path (native spatial dimensions are always
    large), but a small windowed read can legitimately have a spatial ``y``
    or ``x`` dimension of size 1 after coarsening (e.g. the last leftover
    window along an axis) — blindly squeezing it away silently collapses a
    2D array to 1D, corrupting downstream reshapes/broadcasts. Squeezing
    only the named ``time`` dimension (present because every `_load_item`
    call uses ``groupby="solar_day"`` with a single item) avoids that.
    """
    return obj.squeeze("time", drop=True) if "time" in obj.dims else obj


def _native_pixel_windows(
    shape: tuple[int, int],
    window_size: int,
    coarsen_factor: int,
    phase: tuple[int, int] = (0, 0),
) -> list[tuple[int, int, int, int]]:
    """Tile a native pixel grid into non-overlapping, gap-free windows.

    Args:
        shape: (height, width) of the item's full native pixel grid (its
            ``proj:shape`` STAC property).
        window_size: Window edge length in native pixels. Must be a positive
            exact multiple of *coarsen_factor* — this is the constraint that
            keeps `_build_native_masks`'s ``coarsen(...).mean()`` from ever
            having to trim a partial block *inside* a window (trimming may
            only ever happen, if at all, at the tile's true outer edge,
            exactly like today's non-windowed behaviour).
        coarsen_factor: The coarsen factor that will be applied within each
            window's own processing.
        phase: (row_phase, col_phase) — shifts every window/coarsen-group
            boundary so it falls at a pixel index congruent to
            ``phase[i] % coarsen_factor`` instead of 0. **Required to be
            nonzero in production** (see `GfmRasterProcessor._compute_window_phase`):
            the *unwindowed* reference load snaps ``self.bbox`` to the native
            pixel grid at an origin that generally does **not** coincide with
            the tile's own ``proj:transform`` origin (confirmed by direct
            measurement — an accident of where the query bbox's corners land
            on the native lattice, not a property of the tile). Since
            ``coarsen(...).mean()`` groups pixels starting from local index 0
            of whatever array it's given, matching the reference bit-for-bit
            requires windows to start at the SAME phase the reference
            happens to use, not phase (0, 0) — an earlier version of this
            function assumed phase (0, 0) and was found to diverge
            substantially at real flood/water boundaries as a result.
            Defaults to (0, 0) for standalone/unit testing of the pure tiling
            logic in isolation.

    Returns:
        List of (row_start, row_end, col_start, col_end) tuples in row-major
        order. The union of all windows covers the full grid exactly once
        (no gaps, no overlaps). With phase (0, 0), row_start/col_start never
        go negative and row_end/col_end never exceed *shape*. With a nonzero
        phase, the **first** window on an axis may start at a *negative*
        index, and/or the **last** window may end *beyond* the axis length —
        by exactly enough pixels to complete one coarsen group, never more.
        This mirrors the unwindowed reference's own behaviour exactly: its
        coarsen groups near the tile's true edges are completed using
        out-of-tile pixels that read as nodata (the tile's true edge is deep
        inside the much larger padded/ballooned array the reference actually
        loads, so it is never independently trimmed there either — see
        `GfmRasterProcessor._compute_window_phase`). Callers must NOT clamp
        these negative/overshooting indices back into `[0, length)` — doing
        so would silently reintroduce a too-small coarsen group that
        `boundary="trim"` discards entirely.

    Raises:
        ValueError: If *window_size* or *coarsen_factor* isn't a positive
            integer, or *window_size* isn't an exact multiple of
            *coarsen_factor*.
    """
    if coarsen_factor < 1:
        raise ValueError(f"coarsen_factor must be a positive integer, got {coarsen_factor}")
    if window_size <= 0 or window_size % coarsen_factor != 0:
        raise ValueError(
            f"window_size ({window_size}) must be a positive exact multiple of "
            f"coarsen_factor ({coarsen_factor}), so an interior coarsen block "
            "never straddles a window seam"
        )
    height, width = shape

    def _axis_ranges(length: int, axis_phase: int) -> list[tuple[int, int]]:
        ph = axis_phase % coarsen_factor
        # Anchor the first boundary one coarsen-group *before* phase 0 (never
        # a bare 0..ph sliver, which boundary="trim" would discard entirely —
        # see the tiny-window bug this fixes) so the very first coarsen group
        # is a full-sized group straddling the tile's true start edge,
        # exactly like the reference's own first group does.
        start = (ph - coarsen_factor) if ph > 0 else 0
        ranges: list[tuple[int, int]] = []
        while start < length:
            end = start + window_size
            if end >= length:
                remainder = (length - start) % coarsen_factor
                if remainder != 0:
                    end = length + (coarsen_factor - remainder)
                else:
                    end = length
                ranges.append((start, end))
                break
            ranges.append((start, end))
            start = end
        return ranges

    phase_row, phase_col = phase
    row_ranges = _axis_ranges(height, phase_row)
    col_ranges = _axis_ranges(width, phase_col)

    windows: list[tuple[int, int, int, int]] = []
    for row_start, row_end in row_ranges:
        for col_start, col_end in col_ranges:
            windows.append((row_start, row_end, col_start, col_end))
    return windows


def _window_grid_extent(windows: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    """Native pixel-index range spanning the union of all *windows*."""
    arr = np.array(windows)
    return int(arr[:, 0].min()), int(arr[:, 1].max()), int(arr[:, 2].min()), int(arr[:, 3].max())


def _coarsened_axis_coords(
    global_start: int,
    n_groups: int,
    coarsen_factor: int,
    origin: float,
    pixel_size: float,
) -> np.ndarray:
    """Analytical pixel-center coordinates for coarsened groups, without loading data.

    Matches `coarsen(...).mean()`'s `coord_func="mean"` closed form exactly.
    Computed analytically so the assembly buffer's coordinate axis stays
    well-defined even if a window's tile read was skipped (avoids NaN
    coordinates that would break rioxarray's transform inference).
    """
    group_index = np.arange(n_groups, dtype=np.float64)
    center = global_start + group_index * coarsen_factor + (coarsen_factor - 1) / 2.0
    return origin + (center + 0.5) * pixel_size


def _window_native_bounds(
    window: tuple[int, int, int, int],
    transform_coeffs: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float]:
    """Return exact pixel-center bounds (x_min, y_min, x_max, y_max) for a window.

    Uses the item's own ``proj:transform`` (GDAL/rasterio 6-element affine:
    ``x = c + col*a + row*b``, ``y = f + col*d + row*e``) so the bounds are
    derived from real per-item metadata rather than an assumed/hardcoded
    tile shape. GFM Equi7 tiles are axis-aligned (``b == d == 0``); this
    helper assumes that (holds for every sampled tile — see module docstring
    note above) and raises if it doesn't.

    Args:
        window: (row_start, row_end, col_start, col_end) pixel-index range
            (row_end/col_end exclusive), as produced by `_native_pixel_windows`.
        transform_coeffs: The item's ``proj:transform`` STAC property
            (6-element affine, GDAL/rasterio ``(a, b, c, d, e, f)`` order).

    Returns:
        (x_min, y_min, x_max, y_max) in the item's native CRS, at pixel
        *centers* (not edges) — matching the pixel-center convention
        ``odc.stac.load`` uses for its returned x/y coordinates.
    """
    row_start, row_end, col_start, col_end = window
    a, b, c, d, e, f = transform_coeffs
    if b != 0 or d != 0:
        raise ValueError(f"_window_native_bounds assumes an axis-aligned transform (b == d == 0), got b={b}, d={d}")
    x0 = c + (col_start + 0.5) * a
    x1 = c + (col_end - 0.5) * a
    y0 = f + (row_start + 0.5) * e
    y1 = f + (row_end - 0.5) * e
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _window_bbox_4326(
    window: tuple[int, int, int, int],
    transform_coeffs: tuple[float, float, float, float, float, float],
    wkt2: str,
    *,
    margin_px: int = 2,
) -> tuple[float, float, float, float]:
    """Convert one native pixel window to an enclosing EPSG:4326 bbox.

    Pads the window by *margin_px* native pixels on every side (no clamping
    to non-negative/in-bounds indices — a request that extends before index 0
    or beyond the tile's true shape is harmless and, for the phase-aligned
    edge windows `_native_pixel_windows` produces, deliberate: it mirrors the
    unwindowed reference's own out-of-tile nodata inclusion at the tile's true
    edges, see `GfmRasterProcessor._compute_window_phase`), then transforms
    all **four corners** of the padded native-CRS bounds to EPSG:4326 and
    returns their enclosing axis-aligned bbox.

    Using all four corners (not just two opposite ones) matters: GFM's Equi7
    projection (azimuthal equidistant) is not conformal to lon/lat, so a
    straight-edged native rectangle does not generally map to a
    straight-edged lon/lat rectangle. Taking the min/max of all four
    transformed corners guarantees the returned bbox fully *encloses* the
    window (never under-covers it); it may be a slight overestimate, which
    is fine — the caller crops the loaded data back to the window's exact
    pixel-center bounds afterwards (`_window_native_bounds`), so this bbox
    only has to be "big enough", never pixel-exact.

    Args:
        window: (row_start, row_end, col_start, col_end) pixel-index range.
        transform_coeffs: The item's ``proj:transform`` STAC property.
        wkt2: The item's ``proj:wkt2`` STAC property (source CRS as WKT2).
        margin_px: Padding, in native pixels, added on every side before
            transforming — a safety margin against any rounding in the
            bbox-to-pixel snap `odc.stac.load` performs internally (the
            crop back to exact bounds afterwards is what actually
            guarantees correctness; this margin just makes sure the loaded
            data is a superset of what's needed).

    Returns:
        (west, south, east, north) in EPSG:4326.
    """
    import pyproj

    row_start, row_end, col_start, col_end = window
    padded_window = (row_start - margin_px, row_end + margin_px, col_start - margin_px, col_end + margin_px)
    x0, y0, x1, y1 = _window_native_bounds(padded_window, transform_coeffs)

    crs_src = pyproj.CRS.from_wkt(wkt2)
    transformer = pyproj.Transformer.from_crs(crs_src, "EPSG:4326", always_xy=True)
    corners_x = [x0, x0, x1, x1]
    corners_y = [y0, y1, y0, y1]
    lons, lats = transformer.transform(corners_x, corners_y)
    return (min(lons), min(lats), max(lons), max(lats))


def _crop_to_native_bounds(
    dataset: "xr.Dataset",
    bounds: tuple[float, float, float, float],
    pixel_size: float,
) -> "xr.Dataset":
    """Crop a loaded (native-CRS) dataset to an exact window's pixel-center bounds.

    This is the step that actually guarantees no seam corruption: regardless
    of how ``odc.stac.load`` snapped the padded request bbox to its own pixel
    grid, this crops by coordinate *label* (the dataset's real ``x``/``y``
    coords, already in the tile's native CRS since ``_load_item`` is called
    with ``crs=crs_src``) back to precisely the intended window — no more,
    no less. A quarter-pixel tolerance absorbs floating-point coordinate
    jitter without ever being large enough to admit a neighbouring pixel.

    Args:
        dataset: Dataset returned by `_load_item` (native CRS, pixel-center
            x/y coordinates).
        bounds: (x_min, y_min, x_max, y_max) pixel-center bounds from
            `_window_native_bounds` (unpadded — the exact window, not the
            margin-padded request bbox).
        pixel_size: Native pixel size (metres), used to size the tolerance.

    Returns:
        The cropped Dataset.
    """
    x_min, y_min, x_max, y_max = bounds
    tol = abs(pixel_size) / 4.0
    return dataset.sel(
        x=slice(x_min - tol, x_max + tol),
        y=slice(y_max + tol, y_min - tol),
    )


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
    usable_sar: bool = True
    # Classified fields
    water_fraction: np.ndarray | None = None
    flood_fraction: np.ndarray | None = None
    reference_water: np.ndarray | None = None
    extra_layers: dict[str, np.ndarray] = field(default_factory=dict)
    diagnostics: dict[str, np.ndarray] = field(default_factory=dict)
    coverage: GfmCoverageDiagnostics | None = None
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
    diagnostics: dict[str, Path] = field(default_factory=dict)


@dataclass(frozen=True)
class GfmCoverageDiagnostics:
    """Coverage facts for one processed GFM date group."""

    date_token: str
    item_count: int
    item_ids: tuple[str, ...]
    usable_item_count: int
    flood_valid_pixels: int
    water_valid_pixels: int
    likelihood_valid_pixels: int
    exclusion_valid_pixels: int
    advisory_valid_pixels: int
    skipped_windows: int = 0
    aoi_native_bounds: tuple[float, float, float, float] | None = None
    item_bboxes: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)

    @property
    def has_usable_sar(self) -> bool:
        """Whether at least one current flood/water source pixel was loaded."""
        return self.flood_valid_pixels > 0 or self.water_valid_pixels > 0


@dataclass(frozen=True)
class GfmProcessResult:
    """Complete result from the GFM processing pipeline."""

    processed: GfmProcessedTile
    paths: GfmOutputPaths | None
    metadata: TileMetadata
    coverage: GfmCoverageDiagnostics | None = None


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
        window_size: int | None = None,
        persist_diagnostics: bool = False,
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
            window_size: When set (native pixels, classified path only),
                each item's full native tile is processed in a grid of
                pixel-aligned windows of this size instead of eagerly all at
                once, bounding per-item peak memory well below the ~11 GiB
                measured for a full ~15000x15000 Equi7 tile. Must be a
                positive exact multiple of *coarsen_factor*. ``None``
                (default) preserves today's unwindowed behaviour exactly.
                Ignored when *classify* is False. See
                ``scripts/verify_gfm_windowed_correctness.py`` for the
                correctness gate (flood_fraction byte-exact; water_fraction
                has a tiny gated residual).
            persist_diagnostics: Retain classified count rasters on the
                processed tile and write them as validation artifacts.
        """
        self.bbox = bbox
        self.coarsen_factor = coarsen_factor
        self.resampling = resampling
        self.classify = classify
        self.max_retries = max_retries
        self.persist_diagnostics = persist_diagnostics
        if window_size is not None and (window_size <= 0 or window_size % coarsen_factor != 0):
            raise ValueError(
                f"window_size ({window_size}) must be a positive exact multiple of coarsen_factor ({coarsen_factor})"
            )
        self.window_size = window_size
        self.last_coverage: GfmCoverageDiagnostics | None = None
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
        *,
        bands: list[str] | None = None,
    ) -> "xr.Dataset | None":
        """Load one STAC item into memory, retrying transient read failures.

        Args:
            bands: Subset of :data:`GFM_BANDS` to load. Defaults to all of
                them. The classified path loads two smaller groups instead
                (see :data:`_CLASSIFIED_MASK_BANDS` / :data:`_CLASSIFIED_CODE_BANDS`)
                to bound the native-resolution peak footprint.
        """
        import odc.stac

        load_bands = bands if bands is not None else GFM_BANDS

        def _do_load() -> "xr.Dataset":
            xx = odc.stac.load(
                [item],
                bbox=aoi.bounds,
                crs=crs_src,
                bands=load_bands,
                resolution=resolution,
                dtype="uint8",
                groupby="solar_day",
                chunks={},
            )
            # Force eager loading in this worker's thread.  ``chunks={}``
            # produces Dask-backed arrays; without an explicit synchronous
            # scheduler, their inner ``.load()`` computations can be submitted
            # to the outer LocalCluster while it is already running GFM cells.
            # That nested scheduling multiplies native GDAL buffers across
            # workers and defeats the one-cell-per-worker memory bound.
            xx = xx.load(scheduler="synchronous")
            for band in load_bands:
                if band in xx:
                    xx[band].attrs["nodata"] = GFM_NODATA
                    xx[band].attrs["_FillValue"] = GFM_NODATA
            return xx

        return self._retry_read(
            _do_load,
            item_id=item.id,
            context="tile load",
        )

    def _compute_window_phase(
        self,
        item,
        crs_src,
        resolution: float,
        transform_coeffs: tuple[float, float, float, float, float, float],
    ) -> tuple[int, int]:
        """Determine the (row, col) pixel-index phase the unwindowed reference uses.

        ``_build_native_masks``'s ``coarsen(...).mean()`` groups native pixels
        starting from *local* index 0 of whatever array it is given. The
        unwindowed reference load (``odc.stac.load(bbox=self.bbox, ...)``)
        snaps ``self.bbox`` to the native pixel grid at an origin that
        generally does **not** coincide with the tile's own ``proj:transform``
        origin — confirmed by direct measurement to be an accident of where
        ``self.bbox``'s corners land on the native pixel lattice when
        reprojected, not a property of the tile itself. For windowed
        processing to reproduce the *same* coarsen groupings the reference
        happens to use (and therefore match it bit-for-bit), every window
        must be anchored to that same phase — using ``proj:transform``'s raw
        origin (phase 0) was tried first and found to diverge substantially
        at real flood/water boundaries, because the reference's own grouping
        phase is generally nonzero.

        This is a lazy, near-zero-cost STAC metadata probe: ``chunks={}``
        without a trailing ``.load()`` returns real coordinate arrays without
        transferring any pixel data (confirmed by direct measurement: ~0.2s,
        identical coordinates to the real eager load) — this is not a second
        real tile read.

        Returns:
            (row_phase, col_phase), each in ``[0, coarsen_factor)``.
        """
        import odc.stac
        from shapely.geometry import box

        west, south, east, north = self.bbox
        probe = odc.stac.load(
            [item],
            bbox=box(west, south, east, north).bounds,
            crs=crs_src,
            bands=[_CLASSIFIED_MASK_BANDS[0]],
            resolution=resolution,
            dtype="uint8",
            groupby="solar_day",
            chunks={},
        )
        a, _b, c, _d, e, f = transform_coeffs
        x0 = float(probe.x.values[0])
        y0 = float(probe.y.values[0])
        col_index = round((x0 - c) / a - 0.5)
        row_index = round((y0 - f) / e - 0.5)
        return row_index % self.coarsen_factor, col_index % self.coarsen_factor

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
        skipped_window_count = 0
        usable_item_count = 0
        flood_valid_pixels = 0
        water_valid_pixels = 0
        likelihood_valid_pixels = 0
        exclusion_valid_pixels = 0
        advisory_valid_pixels = 0
        item_bboxes = {
            item.id: tuple(float(v) for v in item.bbox) for item in items if getattr(item, "bbox", None) is not None
        }
        try:
            from rasterio.warp import transform_bounds

            aoi_native_bounds = tuple(transform_bounds("EPSG:4326", crs_src, *self.bbox))
        except Exception:
            aoi_native_bounds = None

        if self.window_size is not None:
            logger.debug(
                "GFM windowed processing (window_size={}): flood_fraction is byte-exact vs the "
                "unwindowed reference; water_fraction has a small, bounded, well-understood "
                "residual (max abs diff ~1e-3 to ~4e-3, 7-11 pixels out of ~18M, window-size- "
                "invariant).",
                self.window_size,
            )

        logger.info(
            "Processing {} GFM items (coarsen={}, resampling={}, window_size={})",
            len(items),
            self.coarsen_factor,
            self.resampling,
            self.window_size,
        )

        for idx, item in enumerate(items):
            logger.info(
                "GFM item {}/{} starting: {}",
                idx + 1,
                len(items),
                item.id,
            )
            # Windowed processing: tile this item's full native grid into
            # pixel-aligned windows and run the load -> mask -> coarsen ->
            # reproject body once per window instead of once for the whole
            # tile. `window_size=None` (default) falls back to exactly today's
            # single-AOI, unwindowed behaviour — `windows` has exactly one
            # `None` entry, `window_aoi` is the same `aoi` used before this
            # change, and no crop is applied, so the unwindowed path is
            # byte-for-byte unchanged.
            if self.window_size is None:
                windows: list[tuple[int, int, int, int] | None] = [None]
                transform_coeffs = None
            else:
                native_shape = item.properties.get("proj:shape")
                transform_coeffs_raw = item.properties.get("proj:transform")
                if not native_shape or not transform_coeffs_raw:
                    raise ValueError(
                        f"GFM item {item.id} is missing proj:shape/proj:transform "
                        f"required for windowed processing (window_size={self.window_size})"
                    )
                transform_coeffs = tuple(transform_coeffs_raw[:6])
                # See `_compute_window_phase`: the reference's own grouping
                # phase is generally nonzero, so windows must be anchored to
                # it, not to proj:transform's raw (phase-0) origin.
                phase = self._compute_window_phase(item, crs_src, resolution, transform_coeffs)
                windows = list(
                    _native_pixel_windows(tuple(native_shape), self.window_size, self.coarsen_factor, phase=phase)
                )

            # "assemble, don't buffer": reprojecting each window's masks
            # independently and summing produces a seam artifact (GDAL
            # `average`-resampling needs full-array context). Instead,
            # accumulate each window's coarsened native-CRS masks into one
            # small per-item buffer, then reproject ONCE per item —
            # bit-for-bit matching the unwindowed path. Only the masks group
            # (flood/water/valid); code-bands stay per-window (assembling
            # them would need uncoarsened native-res buffers, defeating the
            # memory win).
            windowed = self.window_size is not None
            if windowed:
                grid_row_start, grid_row_end, grid_col_start, grid_col_end = _window_grid_extent(windows)
                assembly_rows = (grid_row_end - grid_row_start) // self.coarsen_factor
                assembly_cols = (grid_col_end - grid_col_start) // self.coarsen_factor
                a, _b, c, _d, e, f = transform_coeffs
                assembly_y = _coarsened_axis_coords(grid_row_start, assembly_rows, self.coarsen_factor, f, e)
                assembly_x = _coarsened_axis_coords(grid_col_start, assembly_cols, self.coarsen_factor, c, a)
                assembled_flood = np.zeros((assembly_rows, assembly_cols), dtype=np.float32)
                assembled_water = np.zeros((assembly_rows, assembly_cols), dtype=np.float32)
                assembled_valid = np.zeros((assembly_rows, assembly_cols), dtype=np.float32)
                any_window_contributed = False

            skipped_windows = 0
            item_has_sar = False
            for window in windows:
                if window is None:
                    window_aoi = aoi
                    crop_bounds = None
                else:
                    window_bbox = _window_bbox_4326(window, transform_coeffs, item.properties["proj:wkt2"])
                    window_aoi = box(*window_bbox)
                    crop_bounds = _window_native_bounds(window, transform_coeffs)

                # Split the native-resolution load into two smaller groups instead
                # of one eager 6-band load, so at most half of GFM_BANDS' native
                # buffers are resident at any one time. Measured (Phase C.1,
                # /memories/repo/gfm-investigation.md): the single 6-band load
                # followed by _build_native_masks (below) while it's still
                # resident together accounted for the entire ~15 GiB per-cell
                # peak — everything downstream is negligible by comparison, since
                # the canonical/harmonised grid is far smaller than the native one.
                xx_masks = self._load_item(item, window_aoi, crs_src, resolution, bands=_CLASSIFIED_MASK_BANDS)
                if xx_masks is None:
                    skipped_windows += 1
                    if window is not None:
                        logger.warning(
                            "Skipping GFM window {} for item {} (mask-band tile read failed)", window, item.id
                        )
                    continue
                if crop_bounds is not None:
                    xx_masks = _crop_to_native_bounds(xx_masks, crop_bounds, resolution)

                flood_values = np.asarray(_squeeze_time(xx_masks["ensemble_flood_extent"]).values)
                water_values = np.asarray(_squeeze_time(xx_masks["ensemble_water_extent"]).values)
                flood_valid_pixels += int(np.count_nonzero(flood_values != GFM_NODATA))
                water_valid_pixels += int(np.count_nonzero(water_values != GFM_NODATA))
                item_has_sar |= bool(np.any(flood_values != GFM_NODATA) or np.any(water_values != GFM_NODATA))

                # Build per-class 0/1 masks at native resolution, then mean-pool to
                # the coarsened grid. Mean-pooling a binary mask yields the fraction
                # of sub-pixels in each class — the correct way to downsample nominal
                # codes, and consistent with the average reproject that follows. A
                # categorical max would rank codes by number (and let nodata=255 win
                # every mixed block), which is meaningless for class labels.
                flood_mask_native, water_mask_native, valid_mask_native = self._build_native_masks(
                    xx_masks["ensemble_flood_extent"],
                    xx_masks["ensemble_water_extent"],
                    xx_masks["reference_water_mask"],
                    self.coarsen_factor,
                )
                reference_water_band = xr.Dataset(
                    {"reference_water": _squeeze_time(xx_masks["reference_water_mask"])}
                ).rio.write_crs(crs_src)

                if windowed:
                    # Place this window's contribution into the per-item
                    # assembly buffer instead of reprojecting it immediately
                    # (see the "assemble, don't buffer" note above).
                    row_off = (window[0] - grid_row_start) // self.coarsen_factor
                    col_off = (window[2] - grid_col_start) // self.coarsen_factor
                    flood_2d = _squeeze_time(flood_mask_native).values
                    row_len, col_len = flood_2d.shape
                    assembled_flood[row_off : row_off + row_len, col_off : col_off + col_len] = flood_2d
                    assembled_water[row_off : row_off + row_len, col_off : col_off + col_len] = _squeeze_time(
                        water_mask_native
                    ).values
                    assembled_valid[row_off : row_off + row_len, col_off : col_off + col_len] = _squeeze_time(
                        valid_mask_native
                    ).values
                    any_window_contributed = True
                    masks_ll = None
                else:
                    masks = xr.Dataset(
                        {
                            "flood": flood_mask_native,
                            "water": water_mask_native,
                            "valid": valid_mask_native,
                        }
                    ).rio.write_crs(crs_src)
                    # Reproject directly onto the ~80 m global grid
                    # (pre-computed snapped bounds/transform). This ensures
                    # all items accumulate on the same aligned grid — no
                    # double reprojection needed at harmonisation time.
                    masks_ll = self._reproject_to_canonical_grid(masks)
                    del masks

                reference_water_ll = self._reproject_codes_to_canonical_grid(reference_water_band)
                # Free the first group's native buffers (up to ~15 GiB in the
                # worst case, unwindowed) before loading the second group.
                del xx_masks, flood_mask_native, water_mask_native, valid_mask_native, reference_water_band

                xx_codes = self._load_item(item, window_aoi, crs_src, resolution, bands=_CLASSIFIED_CODE_BANDS)
                if xx_codes is None:
                    skipped_windows += 1
                    if window is not None:
                        logger.warning(
                            "Skipping GFM window {} for item {} (code-band tile read failed)", window, item.id
                        )
                    continue
                if crop_bounds is not None:
                    xx_codes = _crop_to_native_bounds(xx_codes, crop_bounds, resolution)

                likelihood_values = np.asarray(_squeeze_time(xx_codes["ensemble_likelihood"]).values)
                exclusion_values = np.asarray(_squeeze_time(xx_codes["exclusion_mask"]).values)
                advisory_values = np.asarray(_squeeze_time(xx_codes["advisory_flags"]).values)
                likelihood_valid_pixels += int(np.count_nonzero(likelihood_values != GFM_NODATA))
                exclusion_valid_pixels += int(np.count_nonzero(exclusion_values != GFM_NODATA))
                advisory_valid_pixels += int(np.count_nonzero(advisory_values != GFM_NODATA))

                code_bands = xr.Dataset(
                    {
                        "exclusion_mask": _squeeze_time(xx_codes["exclusion_mask"]),
                        "advisory_flags": _squeeze_time(xx_codes["advisory_flags"]),
                    }
                ).rio.write_crs(crs_src)
                likelihood_band = xr.Dataset(
                    {"ensemble_likelihood": _squeeze_time(xx_codes["ensemble_likelihood"]).astype("float32")}
                ).rio.write_crs(crs_src)

                codes_ll = self._reproject_codes_to_canonical_grid(code_bands)
                likelihood_ll = self._reproject_likelihood_to_canonical_grid(likelihood_band)
                del xx_codes, code_bands, likelihood_band

                ref_codes = np.squeeze(reference_water_ll["reference_water"].values).astype(np.uint8)
                excl_codes = np.squeeze(codes_ll["exclusion_mask"].values).astype(np.uint8)
                advisory = np.squeeze(codes_ll["advisory_flags"].values).astype(np.uint8)
                likelihood = np.squeeze(likelihood_ll["ensemble_likelihood"].values).astype(np.float32)

                # Code-band accumulators are independent of the masks-group
                # accumulators above (initialised on their own first valid
                # (item, window) pair) — still accumulated per window/item,
                # exactly like an extra STAC item, unaffected by the masks
                # assembly change.
                if reference_water_codes is None:
                    reference_water_codes = ref_codes.copy()
                    exclusion_codes = excl_codes.copy()
                    advisory_flags = advisory.copy()
                    ensemble_likelihood = likelihood.copy()
                else:
                    reference_water_codes = _masked_max(reference_water_codes, ref_codes, GFM_NODATA)
                    exclusion_codes = _masked_max(exclusion_codes, excl_codes, GFM_NODATA)
                    advisory_flags = _masked_or(advisory_flags, advisory, GFM_NODATA)
                    ensemble_likelihood = np.fmax(ensemble_likelihood, likelihood)

                if not windowed:
                    # Unwindowed path: exactly one "window" == the whole
                    # tile, so per-window accumulation here IS per-item
                    # accumulation — unchanged from before this fix.
                    flood_frac = np.squeeze(masks_ll["flood"].fillna(0.0).values.astype("float32"))
                    water_frac = np.squeeze(masks_ll["water"].fillna(0.0).values.astype("float32"))
                    valid_frac = np.squeeze(masks_ll["valid"].fillna(0.0).values.astype("float32"))
                    if flood_count is None:
                        shape = flood_frac.shape
                        flood_count = np.zeros(shape, dtype=np.float32)
                        water_count = np.zeros(shape, dtype=np.float32)
                        valid_count = np.zeros(shape, dtype=np.float32)
                    ref_coords = masks_ll["flood"].coords
                    ref_dims = masks_ll["flood"].dims
                    flood_count += flood_frac
                    water_count += water_frac
                    valid_count += valid_frac
                    del flood_frac, water_frac, valid_frac

                del (
                    masks_ll,
                    reference_water_ll,
                    codes_ll,
                    likelihood_ll,
                    ref_codes,
                    excl_codes,
                    advisory,
                    likelihood,
                )

            skipped_window_count += skipped_windows
            if item_has_sar:
                usable_item_count += 1

            if windowed and any_window_contributed:
                # One reprojection for the item's fully assembled native-CRS
                # masks — see the "assemble, don't buffer" note above.
                assembly_coords = {"y": assembly_y, "x": assembly_x}
                masks_full = xr.Dataset(
                    {
                        "flood": xr.DataArray(assembled_flood, dims=("y", "x"), coords=assembly_coords),
                        "water": xr.DataArray(assembled_water, dims=("y", "x"), coords=assembly_coords),
                        "valid": xr.DataArray(assembled_valid, dims=("y", "x"), coords=assembly_coords),
                    }
                ).rio.write_crs(crs_src)
                masks_ll = self._reproject_to_canonical_grid(masks_full)
                flood_frac = np.squeeze(masks_ll["flood"].fillna(0.0).values.astype("float32"))
                water_frac = np.squeeze(masks_ll["water"].fillna(0.0).values.astype("float32"))
                valid_frac = np.squeeze(masks_ll["valid"].fillna(0.0).values.astype("float32"))
                if flood_count is None:
                    shape = flood_frac.shape
                    flood_count = np.zeros(shape, dtype=np.float32)
                    water_count = np.zeros(shape, dtype=np.float32)
                    valid_count = np.zeros(shape, dtype=np.float32)
                ref_coords = masks_ll["flood"].coords
                ref_dims = masks_ll["flood"].dims
                flood_count += flood_frac
                water_count += water_frac
                valid_count += valid_frac
                del masks_full, masks_ll, flood_frac, water_frac, valid_frac

            if skipped_windows:
                logger.warning(
                    "GFM item {} had {}/{} window(s) skipped due to tile-read failures — "
                    "its contribution to the accumulated grid may be spatially incomplete",
                    item.id,
                    skipped_windows,
                    len(windows),
                )
            logger.info(
                "GFM item {}/{} processed: {} window(s), {} skipped",
                idx + 1,
                len(items),
                len(windows),
                skipped_windows,
            )

        if flood_count is None or valid_count is None:
            logger.warning("No usable SAR data found in {} items", len(items))
            shape = (self._dst_height, self._dst_width)
            processed = GfmProcessedTile(
                water_fraction=np.full(shape, np.nan, dtype=np.float32),
                flood_fraction=np.full(shape, np.nan, dtype=np.float32),
                reference_water=np.full(shape, GFM_NODATA, dtype=np.uint8),
                extra_layers={
                    "exclusion_mask": np.full(shape, GFM_NODATA, dtype=np.uint8),
                    "advisory_flags": np.full(shape, GFM_NODATA, dtype=np.uint8),
                    "ensemble_likelihood": np.full(shape, GFM_NODATA, dtype=np.uint8),
                },
                transform=self._dst_transform,
                crs="EPSG:4326",
                shape=shape,
                cloud_fraction=1.0,
                usable_sar=False,
            )
            self.last_coverage = GfmCoverageDiagnostics(
                date_token=date_token,
                item_count=len(items),
                item_ids=tuple(str(item.id) for item in items),
                usable_item_count=usable_item_count,
                flood_valid_pixels=flood_valid_pixels,
                water_valid_pixels=water_valid_pixels,
                likelihood_valid_pixels=likelihood_valid_pixels,
                exclusion_valid_pixels=exclusion_valid_pixels,
                advisory_valid_pixels=advisory_valid_pixels,
                skipped_windows=skipped_window_count,
                aoi_native_bounds=aoi_native_bounds,
                item_bboxes=item_bboxes,
            )
            return GfmProcessResult(
                processed=processed,
                paths=None,
                metadata=TileMetadata(
                    event_id=event_id,
                    source_id="gfm",
                    fetch_timestamp=datetime.now(timezone.utc),
                    crs="EPSG:4326",
                    resolution=self.reprojector.target_resolution,
                    bbox=self._snapped_bounds,
                    cloud_fraction=1.0,
                    permanent_water_mask_available=True,
                ),
                coverage=self.last_coverage,
            )

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
        self.last_coverage = GfmCoverageDiagnostics(
            date_token=date_token,
            item_count=len(items),
            item_ids=tuple(str(item.id) for item in items),
            usable_item_count=usable_item_count,
            flood_valid_pixels=flood_valid_pixels,
            water_valid_pixels=water_valid_pixels,
            likelihood_valid_pixels=likelihood_valid_pixels,
            exclusion_valid_pixels=exclusion_valid_pixels,
            advisory_valid_pixels=advisory_valid_pixels,
            skipped_windows=skipped_window_count,
            aoi_native_bounds=aoi_native_bounds,
            item_bboxes=item_bboxes,
        )
        processed = replace(
            processed,
            usable_sar=self.last_coverage.has_usable_sar,
            coverage=self.last_coverage,
            diagnostics=(
                {
                    ENSEMBLE_FLOOD_EXTENT_COUNT: flood_count.astype(np.float32, copy=True),
                    ENSEMBLE_WATER_EXTENT_COUNT: water_count.astype(np.float32, copy=True),
                    VALID_COUNT: valid_count.astype(np.float32, copy=True),
                }
                if self.persist_diagnostics
                else {}
            ),
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

        return GfmProcessResult(processed=processed, paths=paths, metadata=metadata, coverage=self.last_coverage)

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
                    "ensemble_flood_extent": _squeeze_time(xx["ensemble_flood_extent"]),
                    "ensemble_water_extent": _squeeze_time(xx["ensemble_water_extent"]),
                    "reference_water_mask": _squeeze_time(xx["reference_water_mask"]),
                    "exclusion_mask": _squeeze_time(xx["exclusion_mask"]),
                    "ensemble_likelihood": _squeeze_time(xx["ensemble_likelihood"]),
                    "advisory_flags": _squeeze_time(xx["advisory_flags"]),
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

        Each mask is binarized *and* coarsened before moving on to the next,
        rather than binarizing all three first — measured (Phase C.2,
        /memories/repo/gfm-investigation.md) to matter: holding all three
        native-resolution float32 masks (each ~1.8 GiB on a full EQUI7 tile)
        simultaneously before any coarsening was the single largest
        contributor to GFM's per-cell peak memory. Coarsening one mask down to
        its small final size before building the next keeps at most one
        native-resolution float32 buffer resident at a time.

        Further reduction (this + the split ``_load_item`` groups bring the
        measured peak from ~15 GiB to ~11 GiB, still above VIIRS/MODIS levels)
        would require processing each mask in spatial windows/blocks instead
        of eagerly over the whole native tile at once — e.g. a real
        dask-lazy pipeline (``chunks=`` instead of ``chunks={}`` in
        ``_load_item``, deferring ``.compute()`` until after this coarsen-mean
        step) so no single native-resolution band is ever fully materialized.
        Not attempted here: bigger change, and running a dask array inside a
        task already scheduled by the outer distributed cluster needs care
        (see the "nested-scheduler gotcha" in
        ``.github/prompts/plan-gfmPeakMemoryFix.prompt.md``).
        """

        def _binarize_and_coarsen(native_bool_mask: "xr.DataArray") -> "xr.DataArray":
            mask = native_bool_mask.astype("float32")
            if coarsen_factor > 1:
                mask = mask.coarsen(y=coarsen_factor, x=coarsen_factor, boundary="trim").mean()
            return mask

        flood_mask = _binarize_and_coarsen(flood_native == GFM_FLOOD)
        water_mask = _binarize_and_coarsen(water_native == GFM_WATER)
        # The reference-water mask is ancillary/static metadata, not a SAR
        # observation. Counting reference-only pixels here dilutes water/flood
        # fractions wherever a tile carries the reference layer but the SAR
        # classification bands are nodata.
        valid_mask = _binarize_and_coarsen((flood_native != GFM_NODATA) | (water_native != GFM_NODATA))
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
        masks = _squeeze_time(masks)

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
        codes = _squeeze_time(codes)
        return codes.rio.reproject(
            "EPSG:4326",
            nodata=GFM_NODATA,
            resampling=Resampling.nearest,
            shape=(self._dst_height, self._dst_width),
            transform=self._dst_transform,
        )

    def _reproject_likelihood_to_canonical_grid(self, likelihood: "xr.Dataset") -> "xr.Dataset":
        """Reproject native likelihood values to the canonical grid with averaging."""
        likelihood = _squeeze_time(likelihood)
        likelihood["ensemble_likelihood"].attrs["nodata"] = np.nan
        likelihood["ensemble_likelihood"].attrs["_FillValue"] = np.nan
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
            diagnostic_paths = {
                name: _write_tif(array, name, "float32", -9999.0) for name, array in processed.diagnostics.items()
            }
            return GfmOutputPaths(
                ensemble_flood_extent=efe_path,
                reference_water_mask=rwm_path,
                extra=extra_paths,
                diagnostics=diagnostic_paths,
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
        diagnostic_paths = {
            name: _write_tif(array, name, "float32", -9999.0) for name, array in processed.diagnostics.items()
        }

        return GfmOutputPaths(
            water_fraction=wf_path,
            flood_fraction=ff_path,
            reference_water=rw_path,
            extra=extra_paths,
            diagnostics=diagnostic_paths,
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
