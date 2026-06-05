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
    """Configuration for archive paths and settings.

    Attributes:
        archive_root: Root directory for archive storage.
        raw_subdir: Subdirectory for raw (unprocessed) data.
        ml_subdir: Subdirectory for ML-ready data.
        checkpoint_dir: Subdirectory for checkpoint markers.
        default_chunk_size: Default chunk size for Zarr storage.
    """

    model_config = SettingsConfigDict(
        env_prefix="ATLANTIS_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    archive_root: Path = Field(default_factory=lambda: Path.home() / "atlantis-data")
    raw_subdir: str = "raw"
    ml_subdir: str = "ml-ready"
    checkpoint_dir: str = ".checkpoints"
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
    """

    model_config = SettingsConfigDict(
        env_prefix="ATLANTIS_",
        env_file=".env",
        env_file_encoding="utf-8",
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


class AtlantisConfig(BaseSettings):
    """Main configuration for Atlantis.

    Combines all sub-configurations and provides defaults.
    """

    model_config = SettingsConfigDict(
        env_prefix="ATLANTIS_",
        env_file=".env",
        env_file_encoding="utf-8",
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
