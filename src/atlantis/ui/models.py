"""Data models for the Atlantis web UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class FetchProgress:
    """Progress state shared between fetch worker and UI poller.

    The background thread writes to an instance of this, and the NiceGUI
    ``ui.timer`` callback reads it every 200 ms to update progress displays.
    """

    stage: Literal["idle", "searching", "fetching", "harmonising", "plotting", "done", "error"] = "idle"
    message: str = ""
    files: int = 0
    error: str | None = None
    diagnostics: object | None = None


@dataclass
class FetchRequest:
    """User-submitted fetch form data.

    Mirrors the CLI fetch options relevant to the web UI. Source-specific
    fields (e.g. ``viirs_backend``) are only consumed when ``source`` matches.
    """

    event_id: str
    bbox: str  # "west south east north"
    start_date: str  # "YYYY-MM-DD"
    end_date: str
    source: str
    classify: bool = True
    stream: bool = True
    harmonise: bool = False
    plot: bool = False
    strategy: str = "peak"
    viirs_backend: str = "noaa_s3"
    modis_backend: str = "lance_geotiff"
    modis_composite: str = "F2"
    gfm_coarsen_factor: int = 4
    gfm_resampling: str = "average"


@dataclass
class FetchResponse:
    """Result of a completed fetch pipeline."""

    event_id: str
    source_id: str
    output_dir: Path
    files: list[Path] = field(default_factory=list)
    harmonised_path: Path | None = None
    plot_path: Path | None = None
    diagnostics: object | None = None
    error: str | None = None


@dataclass
class EventSummary:
    """Summary of a cached event for the History page listing."""

    event_id: str
    sources: list[str]
    file_count: int
    dates: list[str]
    root: Path
