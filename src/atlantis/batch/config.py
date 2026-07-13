"""Configuration dataclass for the batch processing engine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class BatchConfig:
    """Configuration for a Dask-driven batch run.

    Attributes:
        workers_min: Minimum number of Dask worker processes to keep alive.
        workers_max: Maximum number of Dask worker processes (adaptive ceiling).
        memory_limit_per_worker: Memory cap per worker, e.g. ``"6GB"``.
        dashboard_port: Port for the Bokeh Dask dashboard.
        retries: Number of times Dask re-submits a failed task before marking
            it as permanently failed.
        db_path: Path to the SQLite resume database.
        log_every: Emit a progress line every this many completed granules.
    """

    db_path: Path
    workers_min: int = 2
    workers_max: int = 6
    memory_limit_per_worker: str = "4GB"
    dashboard_port: int = 8787
    retries: int = 3
    log_every: int = 100

    def __post_init__(self) -> None:
        """Validate and normalise configuration after initialisation."""
        self.db_path = Path(self.db_path)
        if self.workers_min < 1:
            raise ValueError("workers_min must be >= 1")
        if self.workers_max < self.workers_min:
            raise ValueError("workers_max must be >= workers_min")
        if self.retries < 0:
            raise ValueError("retries must be >= 0")
        if self.log_every < 1:
            raise ValueError("log_every must be >= 1")
