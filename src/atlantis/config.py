"""Configuration management using Pydantic settings."""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class HarmoniseConfig(BaseSettings):
    """Configuration for harmonisation parameters.

    Attributes:
        target_crs: Target coordinate reference system.
        target_resolution: Target spatial resolution in degrees.
        tile_size: Size of square tiles in pixels for ML models.
        resampling: Resampling method (average, bilinear, nearest).
        normalise_range: Tuple of (min, max) for value normalisation.
    """

    model_config = SettingsConfigDict(
        env_prefix="ATLANTIS_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    target_crs: str = "EPSG:4326"
    target_resolution: float = 0.0002777777777777778  # ~1 arc-second
    tile_size: int = 224
    resampling: Literal["average", "bilinear", "nearest", "cubic"] = "average"
    normalise_range: tuple[float, float] = (0.0, 1.0)


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
        viirs_base_url: Override URL for VIIRS data.
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
    viirs_base_url: str | None = None


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
