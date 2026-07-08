"""Metadata models for tiles and sources."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class TileMetadata(BaseModel):
    """Metadata for a processed tile.

    Attributes:
        event_id: Flood event identifier.
        source_id: Data source identifier (e.g., "gfm", "viirs").
        fetch_timestamp: When the data was fetched.
        crs: Coordinate reference system (e.g., "EPSG:4326").
        resolution: Spatial resolution in CRS units.
        bbox: Tile bounding box as (west, south, east, north).
        cloud_fraction: Fraction of cloud cover (0.0-1.0).
        snow_flag: Whether snow was detected.
        quality_bitmask: Quality flags as bitmask.
        permanent_water_mask_available: Whether a source-specific reference/permanent water layer exists.
    """

    event_id: str
    source_id: str
    fetch_timestamp: datetime
    crs: str = "EPSG:4326"
    resolution: float = 0.0002777777777777778  # ~1 arc-second in degrees
    bbox: tuple[float, float, float, float]
    cloud_fraction: float = Field(ge=0.0, le=1.0, default=0.0)
    snow_flag: bool = False
    quality_bitmask: int = 0
    permanent_water_mask_available: bool = False


class SourceMetadata(BaseModel):
    """Metadata for a data source.

    Attributes:
        source_id: Unique source identifier.
        name: Human-readable name.
        description: Description of the data source.
        url: Base URL or API endpoint.
        license: Data license.
        temporal_coverage: Temporal coverage description.
        spatial_coverage: Spatial coverage description.
    """

    source_id: str
    name: str
    description: str
    url: Optional[str] = None
    license: Optional[str] = None
    temporal_coverage: Optional[str] = None
    spatial_coverage: Optional[str] = None
