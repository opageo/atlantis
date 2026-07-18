"""Tests for configuration management."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from atlantis.config import (
    ArchiveConfig,
    AtlantisConfig,
    BookmarksConfig,
    FetcherConfig,
    HarmoniseConfig,
    get_config,
    reload_config,
)


class TestHarmoniseConfig:
    def test_default_values(self):
        cfg = HarmoniseConfig()
        assert cfg.target_crs == "EPSG:4326"
        assert cfg.target_resolution == pytest.approx(0.016666666666666666)
        assert cfg.target_resolution_arcmin == 1.0
        assert cfg.tile_size == 224
        assert cfg.resampling == "average"
        assert cfg.normalise_range == (0.0, 1.0)

    def test_variable_resampling_defaults(self):
        cfg = HarmoniseConfig()
        assert cfg.variable_resampling["flood_fraction"] == "average"
        assert cfg.variable_resampling["quality_mask"] == "mode"
        assert cfg.variable_resampling["permanent_water"] == "mode"
        assert cfg.variable_resampling["raw"] == "nearest"

    @pytest.mark.parametrize(
        ("env_var", "value", "attr", "expected"),
        [
            ("ATLANTIS_TARGET_RESOLUTION", "0.008333", "target_resolution", 0.008333),
            ("ATLANTIS_TILE_SIZE", "512", "tile_size", 512),
            ("ATLANTIS_RESAMPLING", "nearest", "resampling", "nearest"),
            ("ATLANTIS_NORMALISE_RANGE", "(0.1, 0.9)", "normalise_range", (0.1, 0.9)),
        ],
    )
    def test_env_override(self, monkeypatch, env_var, value, attr, expected):
        monkeypatch.setenv(env_var, value)
        # Reload from env — Pydantic reads env on init
        cfg = HarmoniseConfig()
        assert getattr(cfg, attr) == expected

    def test_invalid_resampling_raises(self):
        with pytest.raises(ValidationError):
            HarmoniseConfig(resampling="invalid_method")


class TestArchiveConfig:
    def test_default_values(self):
        cfg = ArchiveConfig()
        assert cfg.archive_root == str(Path.home() / "atlantis-data")
        assert cfg.store == "datacube.zarr"
        assert cfg.checkpoint_dir == ".checkpoints"
        assert cfg.chunk_size == 256
        assert cfg.shard_size == 2048
        assert cfg.scale_factor == 0.01

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ATLANTIS_ARCHIVE_ROOT", "s3://atlantis/cube")
        monkeypatch.setenv("ATLANTIS_CHUNK_SIZE", "128")
        cfg = ArchiveConfig()
        assert cfg.archive_root == "s3://atlantis/cube"
        assert cfg.chunk_size == 128


class TestBookmarksConfig:
    def test_default_values(self):
        cfg = BookmarksConfig()
        assert cfg.bookmarks_root == "s3://atlantis/assets"
        assert cfg.bookmarks_file == "bookmarks.parquet"
        assert cfg.storage_options == {}

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ATLANTIS_BOOKMARKS_ROOT", "s3://atlantis/bookmarks")
        monkeypatch.setenv("ATLANTIS_BOOKMARKS_FILE", "events.parquet")
        cfg = BookmarksConfig()
        assert cfg.bookmarks_root == "s3://atlantis/bookmarks"
        assert cfg.bookmarks_file == "events.parquet"


class TestFetcherConfig:
    def test_default_values(self):
        cfg = FetcherConfig()
        assert cfg.cache_dir == Path.home() / ".cache" / "atlantis"
        assert cfg.timeout == 300
        assert cfg.max_retries == 3
        assert cfg.viirs_backend == "noaa_s3"
        assert cfg.viirs_format == "tif"
        assert cfg.gfm_api_url is None
        assert cfg.viirs_base_url is None
        assert cfg.viirs_excluded_categories == "fill,cloud,snow_ice,shadow,bareland,vegetation"
        assert cfg.viirs_exclude_extra_codes == ""

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ATLANTIS_CACHE_DIR", "/custom/cache")
        monkeypatch.setenv("ATLANTIS_TIMEOUT", "600")
        monkeypatch.setenv("ATLANTIS_MAX_RETRIES", "5")
        monkeypatch.setenv("ATLANTIS_VIIRS_BACKEND", "gmu_legacy")
        cfg = FetcherConfig()
        assert cfg.cache_dir == Path("/custom/cache")
        assert cfg.timeout == 600
        assert cfg.max_retries == 5
        assert cfg.viirs_backend == "gmu_legacy"

    def test_viirs_excluded_categories_env_override(self, monkeypatch):
        monkeypatch.setenv("ATLANTIS_VIIRS_EXCLUDED_CATEGORIES", "fill,cloud,snow_ice,shadow")
        monkeypatch.setenv("ATLANTIS_VIIRS_EXCLUDE_EXTRA_CODES", "27,38")
        cfg = FetcherConfig()
        assert cfg.viirs_excluded_categories == "fill,cloud,snow_ice,shadow"
        assert cfg.viirs_exclude_extra_codes == "27,38"


class TestAtlantisConfig:
    def test_default_values(self):
        cfg = AtlantisConfig()
        assert cfg.log_level == "INFO"
        assert cfg.verbose is False
        assert isinstance(cfg.harmonise, HarmoniseConfig)
        assert isinstance(cfg.archive, ArchiveConfig)
        assert isinstance(cfg.bookmarks, BookmarksConfig)
        assert isinstance(cfg.fetcher, FetcherConfig)

    def test_log_level_env_override(self, monkeypatch):
        monkeypatch.setenv("ATLANTIS_LOG_LEVEL", "DEBUG")
        cfg = AtlantisConfig()
        assert cfg.log_level == "DEBUG"

    def test_verbose_env_override(self, monkeypatch):
        monkeypatch.setenv("ATLANTIS_VERBOSE", "true")
        cfg = AtlantisConfig()
        assert cfg.verbose is True

    def test_nested_config_preserved(self):
        """Sub-configs should still work after parent env override."""
        cfg = AtlantisConfig()
        assert cfg.harmonise.tile_size == 224
        assert cfg.archive.archive_root == str(Path.home() / "atlantis-data")
        assert cfg.fetcher.viirs_backend == "noaa_s3"


class TestGlobalConfig:
    def test_get_config_singleton(self):
        _instance_before = get_config()
        _instance_after = get_config()
        assert _instance_before is _instance_after

    def test_reload_config_creates_new_instance(self):
        cfg_before = get_config()
        reload_config()
        cfg_after = get_config()
        assert cfg_after is not cfg_before

    def test_reload_after_env_change(self, monkeypatch):
        original = get_config()
        assert original.log_level == "INFO"

        monkeypatch.setenv("ATLANTIS_LOG_LEVEL", "DEBUG")
        reload_config()
        updated = get_config()
        assert updated.log_level == "DEBUG"

        # Cleanup: reset env for other tests
        monkeypatch.delenv("ATLANTIS_LOG_LEVEL", raising=False)
