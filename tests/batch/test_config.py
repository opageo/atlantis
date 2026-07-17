"""Unit tests for BatchConfig."""

import pytest

from atlantis.batch.config import BatchConfig


def test_defaults(tmp_path):
    cfg = BatchConfig(db_path=tmp_path / "test.db")
    assert cfg.workers_min == 2
    assert cfg.workers_max == 6
    assert cfg.memory_limit_per_worker == "4GB"
    assert cfg.dashboard_port == 8787
    assert cfg.retries == 3
    assert cfg.log_every == 100


def test_db_path_coerced_to_path(tmp_path):
    cfg = BatchConfig(db_path=str(tmp_path / "test.db"))
    from pathlib import Path

    assert isinstance(cfg.db_path, Path)


def test_invalid_workers_min(tmp_path):
    with pytest.raises(ValueError, match="workers_min"):
        BatchConfig(db_path=tmp_path / "test.db", workers_min=0)


def test_invalid_workers_max(tmp_path):
    with pytest.raises(ValueError, match="workers_max"):
        BatchConfig(db_path=tmp_path / "test.db", workers_min=4, workers_max=2)


def test_invalid_retries(tmp_path):
    with pytest.raises(ValueError, match="retries"):
        BatchConfig(db_path=tmp_path / "test.db", retries=-1)


def test_invalid_log_every(tmp_path):
    with pytest.raises(ValueError, match="log_every"):
        BatchConfig(db_path=tmp_path / "test.db", log_every=0)
