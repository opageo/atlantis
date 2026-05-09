"""Flood event data model."""

from dataclasses import dataclass, field
from datetime import date


@dataclass
class FloodEvent:
    """Represents a flood event with spatial and temporal bounds.

    Attributes:
        event_id: Unique identifier (e.g., "Valencia", matches Kuro Siwo IDs).
        bbox: Bounding box as (west, south, east, north) in degrees.
        start_date: Start of the flood event.
        end_date: End of the flood event.
        sources: List of data sources to fetch (e.g., ["gfm", "viirs"]).
    """

    event_id: str
    bbox: tuple[float, float, float, float]
    start_date: date
    end_date: date
    sources: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate bounding box and date range."""
        west, south, east, north = self.bbox
        if not (-180 <= west <= 180 and -180 <= east <= 180):
            raise ValueError("Longitude values must be between -180 and 180")
        if not (-90 <= south <= 90 and -90 <= north <= 90):
            raise ValueError("Latitude values must be between -90 and 90")
        if west > east:
            raise ValueError("West longitude must be <= East longitude")
        if south > north:
            raise ValueError("South latitude must be <= North latitude")
        if self.end_date < self.start_date:
            raise ValueError("end_date must be >= start_date")
