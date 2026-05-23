"""Helpers for turning KuroSiwo metadata into Atlantis flood events."""

from datetime import timedelta
from pathlib import Path

import pandas as pd

from atlantis.models.event import FloodEvent

KUROSIWO_REQUIRED_COLUMNS = {
    "flood_case",
    "date_start",
    "date_end",
    "lat_min",
    "lat_max",
    "lon_min",
    "lon_max",
}


def load_kurosiwo_metadata(metadata_path: Path) -> pd.DataFrame:
    """Load and validate the KuroSiwo event metadata CSV."""
    if not metadata_path.exists():
        raise FileNotFoundError(f"KuroSiwo metadata CSV not found: {metadata_path}")

    dataframe = pd.read_csv(metadata_path, parse_dates=["date_start", "date_end"])
    missing = KUROSIWO_REQUIRED_COLUMNS - set(dataframe.columns)
    if missing:
        missing_columns = ", ".join(sorted(missing))
        raise ValueError(f"KuroSiwo metadata is missing required columns: {missing_columns}")

    return dataframe.sort_values("flood_case").reset_index(drop=True)


def build_kurosiwo_flood_events(
    metadata_path: Path,
    *,
    case: str | None = None,
    limit: int | None = None,
    days_before: int = 0,
    days_after: int = 0,
    use_metadata_range: bool = False,
) -> list[FloodEvent]:
    """Build Atlantis flood events from KuroSiwo metadata.

    By default the event window is centered on the KuroSiwo flood-time acquisition
    (`date_end`) because the metadata `date_start -> date_end` span can cover many
    months of SAR baselines and is too broad for practical VIIRS extraction.
    """
    dataframe = load_kurosiwo_metadata(metadata_path)

    if case is not None:
        dataframe = dataframe[dataframe["flood_case"] == case].copy()
        if dataframe.empty:
            raise KeyError(f"KuroSiwo flood case not found in metadata: {case}")

    if limit is not None:
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        dataframe = dataframe.head(limit).copy()

    events: list[FloodEvent] = []
    for row in dataframe.itertuples(index=False):
        if use_metadata_range:
            start_date = row.date_start.date()
            end_date = row.date_end.date()
        else:
            flood_date = row.date_end.date()
            start_date = flood_date - timedelta(days=days_before)
            end_date = flood_date + timedelta(days=days_after)

        events.append(
            FloodEvent(
                event_id=row.flood_case,
                bbox=(float(row.lon_min), float(row.lat_min), float(row.lon_max), float(row.lat_max)),
                start_date=start_date,
                end_date=end_date,
                sources=["viirs"],
            )
        )

    return events
