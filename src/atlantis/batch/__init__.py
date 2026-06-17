"""Public surface of the dataset-agnostic batch engine."""

from __future__ import annotations

from dataclasses import dataclass

from atlantis.batch.config import BatchConfig
from atlantis.batch.orchestrator import run_batch

__all__ = ["BatchConfig", "TaskResult", "run_batch"]


@dataclass(frozen=True)
class TaskResult:
    """Returned by a successful per-task processing function.

    Attributes:
        task_id: Matches the ``task_id`` key in the input task dict.
        output_uri: Full S3 URI of the written COG.
        status: Always ``"DONE"`` for successful results.
    """

    task_id: str
    output_uri: str
    status: str = "DONE"
