"""Configuration management using Pydantic settings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, EnvSettingsSource, SettingsConfigDict


def _parse_tuple(v: str) -> tuple[float, ...]:
    """Parse a Python-style tuple string like ``(0.1, 0.9)`` into a tuple."""
    cleaned = v.strip("()")
    return tuple(float(x.strip()) for x in cleaned.split(","))


class TupleEnvSource(EnvSettingsSource):
    """EnvSettingsSource that handles Python-style tuples in env vars.

    Pydantic-settings tries ``json.loads()`` on complex types, which fails
    for Python-style tuples ``(0.1, 0.9)``.  This subclass converts them
    before parsing.
    """

    def decode_complex_value(self, field_name: str, field: Any, value: Any) -> Any:
        """Decode a string env value that may use Python tuple syntax."""
        if isinstance(value, str) and value.startswith("(") and value.endswith(")"):
            try:
                return json.loads(value.replace("(", "[").replace(")", "]"))
            except json.JSONDecodeError:
                return _parse_tuple(value)
        return super().decode_complex_value(field_name, field, value)


class HarmoniseConfig(BaseSettings):
    """Configuration for harmonisation parameters.

    Attributes:
        target_crs: Target coordinate reference system.
        target_resolution: Target spatial resolution in degrees.
        target_resolution_arcmin: Target resolution expressed in arc-minutes (convenience).
        tile_size: Size of square tiles in pixels for ML models.
        resampling: Default resampling method (average, bilinear, nearest).
        variable_resampling: Per-variable resampling overrides. Defaults:
            water_fraction/flood_fraction->average,
            exclusion_mask/reference_water->mode, raw->nearest.
        normalise_range: Tuple of (min, max) for value normalisation.
        snap_to_global_grid: If True (default), snap output windows to the canonical
            global lat/lon grid anchored at ``(global_grid_origin_lon, global_grid_origin_lat)``
            with spacing ``target_resolution``. This guarantees pixel centres align
            with the 1-arcmin reference grid (``±(k+0.5)/60``) used by ECMWF
            ``Globe_flood_area_*.grb`` and similar products, so AOI windows can be
            stacked field-by-field with global ``field(lat, lon)`` datasets.
        global_grid_origin_lon: Western edge of the global grid (default ``-180.0``).
        global_grid_origin_lat: Northern edge of the global grid (default ``+90.0``).
    """

    model_config = SettingsConfigDict(
        env_prefix="ATLANTIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    target_crs: str = "EPSG:4326"
    target_resolution: float = 0.016666666666666666  # ~1 arc-minute
    target_resolution_arcmin: float = 1.0
    tile_size: int = 224
    resampling: Literal["average", "bilinear", "nearest", "cubic"] = "average"
    variable_resampling: dict[str, Literal["average", "bilinear", "nearest", "cubic", "mode"]] = {
        "water_fraction": "average",
        "flood_fraction": "average",
        # Legacy aliases retained for internal/backward compatibility.
        "quality_mask": "mode",
        "permanent_water": "mode",
        "exclusion_mask": "mode",
        "reference_water": "mode",
        "recurring_flood": "mode",
        "ensemble_likelihood": "average",
        "advisory_flags": "mode",
        "raw": "nearest",
        # GFM native code bands — preserve discrete codes (no averaging).
        "ensemble_flood_extent": "nearest",
        "ensemble_water_extent": "nearest",
        "reference_water_mask": "nearest",
    }
    normalise_range: tuple[float, float] = (0.0, 1.0)
    snap_to_global_grid: bool = True
    global_grid_origin_lon: float = -180.0
    global_grid_origin_lat: float = 90.0

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type,
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        """Customise env source to handle Python tuple syntax."""
        return (
            init_settings,
            TupleEnvSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )


class ArchiveConfig(BaseSettings):
    """Configuration for the consolidated Zarr datacube archive.

    The archive is a single sharded Zarr store with one group per source,
    co-registered on the canonical global 1-arcmin grid. It lives locally or on
    S3 (``ATLANTIS_ARCHIVE_ROOT=s3://bucket/prefix``).

    Attributes:
        archive_root: Root location for archive storage (local path or ``s3://`` URI).
        storage_options: fsspec options for remote roots (credentials, ``anon``, ...).
        store: Name of the consolidated datacube store under the archive root.
        checkpoint_dir: Subdirectory for checkpoint markers.
        chunk_size: Spatial inner-chunk size (pixels) — the data-loader read
            granularity. 256 is power-of-two and ``/32``-friendly for U-Nets and
            still efficient for large-window analysis reads.
        shard_size: Spatial shard size (pixels) — the storage-object granularity;
            must be a multiple of ``chunk_size``. One shard bundles many inner
            chunks into a single (cloud) object.
        scale_factor: CF ``scale_factor`` for ``flood_fraction`` so the uint8
            ``[0, 100]`` storage decodes to float ``[0, 1]`` (CMF-comparable).
        time_epoch: CF epoch (``YYYY-MM-DD``) for the integer ``time`` axis.
    """

    model_config = SettingsConfigDict(
        env_prefix="ATLANTIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    archive_root: str = Field(default_factory=lambda: str(Path.home() / "atlantis-data"))
    storage_options: dict[str, Any] = Field(default_factory=dict)
    store: str = "datacube.zarr"
    checkpoint_dir: str = ".checkpoints"
    chunk_size: int = 256
    shard_size: int = 2048
    scale_factor: float = 0.01
    time_epoch: str = "2020-01-01"


class FetcherConfig(BaseSettings):
    """Configuration for data fetchers.

    Attributes:
        cache_dir: Directory for caching downloaded data.
        timeout: Request timeout in seconds.
        max_retries: Maximum number of retries for failed requests.
        gfm_api_url: Override URL for GFM STAC API.
        viirs_backend: Default VIIRS backend.
        viirs_base_url: Override URL for NOAA VIIRS data.
        viirs_legacy_base_url: Override URL for legacy GMU VIIRS data.
        viirs_format: Default VIIRS data format.
        modis_backend: Default MODIS backend.
        modis_composite: Default MODIS composite (F1 / F1C / F2 / F3).
        modis_lance_primary_base_url: Override URL for the primary LANCE NRT mirror.
        modis_lance_backup_base_url: Override URL for the backup LANCE NRT mirror.
        modis_laads_base_url: Override URL for the LAADS DAAC archive.
    """

    model_config = SettingsConfigDict(
        env_prefix="ATLANTIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    cache_dir: Path = Field(default_factory=lambda: Path.home() / ".cache" / "atlantis")
    timeout: int = 300  # 5 minutes
    max_retries: int = 3
    gfm_api_url: str | None = None
    gfm_coarsen_factor: int = 4
    gfm_resampling: str = "average"
    viirs_backend: Literal["noaa_s3", "gmu_legacy"] = "noaa_s3"
    viirs_base_url: str | None = None
    viirs_legacy_base_url: str | None = None
    viirs_format: Literal["tif", "netcdf", "shapezip", "png"] = "tif"
    modis_backend: Literal["lance_geotiff", "laads_hdf4"] = "lance_geotiff"
    modis_composite: Literal["F1", "F1C", "F2", "F3"] = "F2"
    modis_lance_primary_base_url: str | None = None
    modis_lance_backup_base_url: str | None = None
    modis_laads_base_url: str | None = None


class StacConfig(BaseSettings):
    """Configuration for the STAC layer over the Zarr datacube.

    The STAC layer is a static catalog (``Catalog → one Collection per source →
    one Item per populated date``) that indexes the consolidated ``datacube.zarr``.
    Collections/items use the datacube extension and reference the Zarr store via
    an asset carrying the xarray-assets ``xarray:open_kwargs`` (so an item can be
    opened directly with xpystac/xarray).

    Attributes:
        catalog_id: Root catalog id; per-source collections are ``{id}-{source}``.
        catalog_title: Human-readable catalog title.
        catalog_description: Catalog description.
        catalog_root: Default destination for the written catalog (local dir or
            ``s3://`` URI).
        zarr_media_type: Media type used for the Zarr asset.
        compute_item_bbox: If True, compute each item's bbox from the populated
            (non-fill) pixels of that date; otherwise reuse the source extent.
    """

    model_config = SettingsConfigDict(
        env_prefix="ATLANTIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    catalog_id: str = "atlantis-datacube"
    catalog_title: str = "Atlantis flood datacube"
    catalog_description: str = (
        "STAC catalog over the consolidated Atlantis Zarr datacube — one collection "
        "per source, one item per populated date."
    )
    catalog_root: str = Field(default_factory=lambda: str(Path.home() / "atlantis-data" / "stac"))
    zarr_media_type: str = "application/vnd+zarr"
    compute_item_bbox: bool = True


class VizConfig(BaseSettings):
    """Configuration for the local HoloViz visualization server.

    Attributes:
        variable: Default data variable to render.
        cmap: Default colormap name.
        host: Bind address for the local Panel server.
        port: Port for the local Panel server.
        basemap: Overlay coastlines & country borders (vector features drawn on
            top of the data; requires ``geoviews``/``cartopy``).
        tiles: Add an OSM web-tile basemap under the data (requires ``geoviews``).
        rasterize: Server-side rasterise via datashader (recommended for large
            windows; requires ``datashader``).
        frame_width: Plot frame width in pixels.
    """

    model_config = SettingsConfigDict(
        env_prefix="ATLANTIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    variable: str = "flood_fraction"
    cmap: str = "Blues"
    host: str = "localhost"
    port: int = 5006
    basemap: bool = False
    tiles: bool = False
    rasterize: bool = True
    frame_width: int = 700


class AtlantisConfig(BaseSettings):
    """Main configuration for Atlantis.

    Combines all sub-configurations and provides defaults.
    """

    model_config = SettingsConfigDict(
        env_prefix="ATLANTIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    harmonise: HarmoniseConfig = Field(default_factory=HarmoniseConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)
    fetcher: FetcherConfig = Field(default_factory=FetcherConfig)
    stac: StacConfig = Field(default_factory=StacConfig)
    viz: VizConfig = Field(default_factory=VizConfig)

    # Global settings
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    verbose: bool = False


# Global config instance
_config: AtlantisConfig | None = None


def get_config() -> AtlantisConfig:
    """Get the global Atlantis configuration.

    Returns:
        The global configuration instance.
    """
    global _config
    if _config is None:
        _config = AtlantisConfig()
    return _config


def reload_config() -> AtlantisConfig:
    """Reload the global configuration from environment.

    Returns:
        The reloaded configuration instance.
    """
    global _config
    _config = AtlantisConfig()
    return _config
