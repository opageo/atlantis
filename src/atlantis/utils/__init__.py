"""Utility functions for Atlantis."""

from atlantis.utils.geo import bbox_intersects, tile_bbox, validate_bbox
from atlantis.utils.io import download_file, ensure_dir, get_cache_path
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

__all__ = [
    "bbox_intersects",
    "tile_bbox",
    "validate_bbox",
    "download_file",
    "get_cache_path",
    "ensure_dir",
    "KUROSIWO_DEFAULT_CATALOGUE",
    "KUROSIWO_DEFAULT_METADATA",
    "load_kurosiwo_catalogue",
    "load_kurosiwo_metadata",
    "derive_kurosiwo_metadata",
    "write_kurosiwo_metadata_csv",
    "build_kurosiwo_flood_events",
    "build_kurosiwo_flood_events_from_catalogue",
]
