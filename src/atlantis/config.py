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
            flood_fraction->average, quality_mask->mode, permanent_water->mode, raw->nearest.
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
        "flood_fraction": "average",
        "quality_mask": "mode",
        "permanent_water": "mode",
        "recurring_flood": "mode",
        "raw": "nearest",
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

    The archive is a single Zarr store per layer with one group per source,
    co-registered on the canonical global 1-arcmin grid. It lives locally or on
    S3 (``ATLANTIS_ARCHIVE_ROOT=s3://bucket/prefix``).

    Attributes:
        archive_root: Root location for archive storage (local path or ``s3://`` URI).
        storage_options: fsspec options for remote roots (credentials, ``anon``, ...).
        raw_store: Name of the analysis-ready datacube store (global grid).
        ml_store: Name of the ML-ready datacube store (tiled + sharded).
        checkpoint_dir: Subdirectory for checkpoint markers.
        raw_chunk_size: Spatial chunk size (pixels) for the analysis-ready cube.
        ml_tile_size: Spatial chunk size (pixels) for ML tiles — the data-loader
            read granularity. 256 is power-of-two and ``/32``-friendly for U-Nets.
        ml_shard_size: Spatial shard size (pixels) for the ML cube — the S3 object
            granularity. Must be a multiple of ``ml_tile_size``.
        scale_factor: CF ``scale_factor`` for ``flood_fraction`` so the uint8
            ``[0, 100]`` storage decodes to float ``[0, 1]`` (CMF-comparable).
        time_epoch: CF epoch (``YYYY-MM-DD``) for the integer ``time`` axis.
        default_chunk_size: Deprecated alias retained for compatibility.
    """

    model_config = SettingsConfigDict(
        env_prefix="ATLANTIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    archive_root: str = Field(default_factory=lambda: str(Path.home() / "atlantis-data"))
    storage_options: dict[str, Any] = Field(default_factory=dict)
    raw_store: str = "raw.zarr"
    ml_store: str = "ml-ready.zarr"
    checkpoint_dir: str = ".checkpoints"
    raw_chunk_size: int = 1024
    ml_tile_size: int = 256
    ml_shard_size: int = 2048
    scale_factor: float = 0.01
    time_epoch: str = "2020-01-01"
    default_chunk_size: int = 224


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
