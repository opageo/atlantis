"""Utility functions for Atlantis."""

from atlantis.utils.geo import bbox_intersects, tile_bbox, validate_bbox
from atlantis.utils.io import HtmlResponseError, download_file, ensure_dir, get_cache_path
from atlantis.utils.kurosiwo import (
    KUROSIWO_DEFAULT_CATALOGUE,
    KUROSIWO_DEFAULT_METADATA,
    build_kurosiwo_flood_events,
    build_kurosiwo_flood_events_from_catalogue,
    derive_kurosiwo_metadata,
    load_kurosiwo_catalogue,
    load_kurosiwo_metadata,
    write_kurosiwo_metadata_csv,
)
from atlantis.utils.plot import (
    GFM_ENSEMBLE_FLOOD_EXTENT_CODES,
    GFM_REFERENCE_WATER_MASK_CODES,
    MODIS_RAW_CODES,
    VIIRS_RAW_CODES,
    date_from_filename,
    legend_patches,
    pixel_stats_classified,
    pixel_stats_raw,
    plot_classified,
    plot_raw,
)

__all__ = [
    "bbox_intersects",
    "tile_bbox",
    "validate_bbox",
    "download_file",
    "get_cache_path",
    "ensure_dir",
    "HtmlResponseError",
    "KUROSIWO_DEFAULT_CATALOGUE",
    "KUROSIWO_DEFAULT_METADATA",
    "load_kurosiwo_catalogue",
    "load_kurosiwo_metadata",
    "derive_kurosiwo_metadata",
    "write_kurosiwo_metadata_csv",
    "build_kurosiwo_flood_events",
    "build_kurosiwo_flood_events_from_catalogue",
    "VIIRS_RAW_CODES",
    "GFM_ENSEMBLE_FLOOD_EXTENT_CODES",
    "GFM_REFERENCE_WATER_MASK_CODES",
    "MODIS_RAW_CODES",
    "date_from_filename",
    "legend_patches",
    "pixel_stats_raw",
    "pixel_stats_classified",
    "plot_raw",
    "plot_classified",
]
