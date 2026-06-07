"""Helpers for turning KuroSiwo catalogue data into Atlantis metadata and events."""

from datetime import timedelta
from pathlib import Path

import geopandas as gpd
import pandas as pd

from atlantis.models.event import FloodEvent

KUROSIWO_DEFAULT_CATALOGUE = Path("assets/ks_catalogue.gpkg")
KUROSIWO_DEFAULT_METADATA = Path("data/metadata/kurosiwo_metadata_v1.csv")
AREA_CRS = "EPSG:6933"
EXTENT_CRANK = 1

KUROSIWO_REQUIRED_COLUMNS = {
    "flood_case",
    "date_start",
    "date_end",
    "lat_min",
    "lat_max",
    "lon_min",
    "lon_max",
}


def is_lfs_pointer(path: Path) -> bool:
    """Check whether a file is a Git LFS pointer instead of real content."""
    try:
        with path.open("r", encoding="utf-8") as file_handle:
            first_line = file_handle.readline()
    except UnicodeDecodeError:
        return False
    except OSError:
        return False
    return first_line.startswith("version https://git-lfs.github.com/spec")


def load_kurosiwo_catalogue(catalogue_path: Path) -> gpd.GeoDataFrame:
    """Load the KuroSiwo GeoPackage catalogue.

    Args:
        catalogue_path: Path to the KuroSiwo GeoPackage catalogue.

    Returns:
        GeoDataFrame with the raw KuroSiwo catalogue.
    """
    if not catalogue_path.exists():
        raise FileNotFoundError(f"KuroSiwo catalogue not found: {catalogue_path}")
    if is_lfs_pointer(catalogue_path):
        raise RuntimeError(
            f"KuroSiwo catalogue at {catalogue_path} is a Git LFS pointer. Run `git lfs pull` before deriving metadata."
        )
    return gpd.read_file(catalogue_path)


def derive_kurosiwo_metadata(catalogue_path: Path, extent_crank: int = EXTENT_CRANK) -> pd.DataFrame:
    """Derive per-event KuroSiwo metadata directly from the catalogue.

    Args:
        catalogue_path: Path to the KuroSiwo GeoPackage catalogue.
        extent_crank: Product type to use for flood extent computation.

    Returns:
        DataFrame with one row per KuroSiwo event.
    """
    catalogue = load_kurosiwo_catalogue(catalogue_path)
    catalogue_wgs84 = catalogue.to_crs("EPSG:4326")
    catalogue_area = catalogue.to_crs(AREA_CRS).copy()
    catalogue_area["patch_area_km2"] = catalogue_area.geometry.area / 1_000_000

    records: list[dict[str, object]] = []
    for actid in sorted(catalogue["actid"].unique()):
        event_rows = catalogue[catalogue["actid"] == actid]
        event_geo = catalogue_wgs84[catalogue_wgs84["actid"] == actid]
        event_area = catalogue_area[catalogue_area["actid"] == actid]

        flood_mask = event_rows["master"].fillna(False).astype(bool)
        flood_rows = event_rows[flood_mask]
        preflood_rows = event_rows[~flood_mask]
        lon_min, lat_min, lon_max, lat_max = event_geo.total_bounds

        area_flood_mask = event_area["master"].fillna(False).astype(bool)
        flooded_rows = event_area[
            area_flood_mask
            & (event_area["crank"] == extent_crank)
            & (event_area["pflood"].notna())
            & (event_area["pflood"] > 0)
        ]
        extent_km2 = (flooded_rows["patch_area_km2"] * flooded_rows["pflood"] / 100).sum()

        date_start = pd.to_datetime(preflood_rows["source_date"]).min()
        date_end = pd.to_datetime(flood_rows["source_date"]).min()

        records.append(
            {
                "flood_case": f"KuroSiwo_{int(actid):03d}",
                "date_start": date_start.date() if pd.notna(date_start) else None,
                "date_end": date_end.date() if pd.notna(date_end) else None,
                "lat_min": round(float(lat_min), 4),
                "lat_max": round(float(lat_max), 4),
                "lon_min": round(float(lon_min), 4),
                "lon_max": round(float(lon_max), 4),
                "max_flood_extent_km2": round(float(extent_km2), 1),
                "date_of_max_flood_extent": date_end.date() if pd.notna(date_end) else None,
            }
        )

    return pd.DataFrame(records).sort_values("flood_case").reset_index(drop=True)


def write_kurosiwo_metadata_csv(catalogue_path: Path, output_path: Path) -> Path:
    """Derive and write KuroSiwo metadata as CSV.

    Args:
        catalogue_path: Path to the KuroSiwo GeoPackage catalogue.
        output_path: Destination CSV path.

    Returns:
        Path to the written CSV file.
    """
    dataframe = derive_kurosiwo_metadata(catalogue_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    dataframe.to_csv(output_path, index=False)
    return output_path


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


def build_kurosiwo_flood_events_from_dataframe(
    dataframe: pd.DataFrame,
    *,
    case: str | None = None,
    limit: int | None = None,
    days_before: int = 0,
    days_after: int = 0,
    use_metadata_range: bool = False,
) -> list[FloodEvent]:
    """Build Atlantis flood events from an in-memory KuroSiwo metadata table."""
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
            start_date = row.date_start.date() if hasattr(row.date_start, "date") else row.date_start
            end_date = row.date_end.date() if hasattr(row.date_end, "date") else row.date_end
        else:
            flood_date = row.date_end.date() if hasattr(row.date_end, "date") else row.date_end
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
    return build_kurosiwo_flood_events_from_dataframe(
        dataframe,
        case=case,
        limit=limit,
        days_before=days_before,
        days_after=days_after,
        use_metadata_range=use_metadata_range,
    )


def build_kurosiwo_flood_events_from_catalogue(
    catalogue_path: Path,
    *,
    case: str | None = None,
    limit: int | None = None,
    days_before: int = 0,
    days_after: int = 0,
    use_metadata_range: bool = False,
) -> list[FloodEvent]:
    """Build Atlantis flood events directly from the KuroSiwo catalogue."""
    dataframe = derive_kurosiwo_metadata(catalogue_path)
    return build_kurosiwo_flood_events_from_dataframe(
        dataframe,
        case=case,
        limit=limit,
        days_before=days_before,
        days_after=days_after,
        use_metadata_range=use_metadata_range,
    )
