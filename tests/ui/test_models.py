"""Tests for UI data models."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlantis.ui.models import EventSummary, FetchProgress, FetchRequest, FetchResponse


class TestFetchProgress:
    """Tests for FetchProgress progress-tracking dataclass."""

    def test_default_values(self) -> None:
        """All fields have sensible defaults for idle state."""
        p = FetchProgress()
        assert p.stage == "idle"
        assert p.message == ""
        assert p.files == 0
        assert p.error is None
        assert p.diagnostics is None

    def test_custom_values(self) -> None:
        """Fields accept explicit values."""
        p = FetchProgress(stage="searching", message="Looking up...", files=3, error="timeout")
        assert p.stage == "searching"
        assert p.message == "Looking up..."
        assert p.files == 3
        assert p.error == "timeout"

    def test_stage_literal_error(self) -> None:
        """'error' is a valid stage value."""
        p = FetchProgress(stage="error", error="BOOM")
        assert p.stage == "error"

    def test_stage_literal_done(self) -> None:
        """'done' is a valid stage value."""
        p = FetchProgress(stage="done", message="Finished", files=10)
        assert p.stage == "done"
        assert p.files == 10

    def test_diagnostics_can_be_any_object(self) -> None:
        """diagnostics accepts an arbitrary object (duck-typed)."""
        diag = type("Diag", (), {"missing_aoi_coverage": True})()
        p = FetchProgress(diagnostics=diag)
        assert p.diagnostics.missing_aoi_coverage is True


class TestFetchRequest:
    """Tests for FetchRequest form-to-CLI bridge."""

    def test_minimal_creation(self) -> None:
        """A FetchRequest can be created with required fields."""
        r = FetchRequest(
            event_id="Ev1",
            bbox="0 0 1 1",
            start_date="2024-01-01",
            end_date="2024-01-02",
            source="viirs",
        )
        assert r.event_id == "Ev1"
        assert r.source == "viirs"
        assert r.classify is True
        assert r.stream is True
        assert r.harmonise is False
        assert r.plot is False

    def test_all_defaults(self) -> None:
        """Verify default values for optional flags."""
        r = FetchRequest(event_id="X", bbox="0 0 1 1", start_date="2024-01-01", end_date="2024-01-02", source="viirs")
        assert r.strategy == "peak"
        assert r.viirs_backend == "noaa_s3"
        assert r.modis_backend == "lance_geotiff"
        assert r.modis_composite == "F2"
        assert r.gfm_coarsen_factor == 4
        assert r.gfm_resampling == "average"

    def test_source_specific_overrides(self) -> None:
        """Source-specific fields are set independently."""
        r = FetchRequest(
            event_id="E2",
            bbox="10 20 30 40",
            start_date="2024-06-01",
            end_date="2024-06-05",
            source="modis",
            classify=False,
            stream=False,
            harmonise=True,
            plot=True,
            strategy="all",
            modis_backend="laads_hdf4",
            modis_composite="F3",
        )
        assert r.source == "modis"
        assert r.classify is False
        assert r.stream is False
        assert r.harmonise is True
        assert r.plot is True
        assert r.strategy == "all"
        assert r.modis_backend == "laads_hdf4"
        assert r.modis_composite == "F3"

    def test_gfm_overrides(self) -> None:
        """GFM-specific fields can be set."""
        r = FetchRequest(
            event_id="gf",
            bbox="0 0 1 1",
            start_date="2024-01-01",
            end_date="2024-01-02",
            source="gfm",
            gfm_coarsen_factor=8,
            gfm_resampling="bilinear",
        )
        assert r.gfm_coarsen_factor == 8
        assert r.gfm_resampling == "bilinear"


class TestFetchResponse:
    """Tests for FetchResponse result container."""

    def test_minimal_creation(self, tmp_path: Path) -> None:
        """A FetchResponse can store event and source identifiers."""
        resp = FetchResponse(event_id="E1", source_id="viirs", output_dir=tmp_path)
        assert resp.event_id == "E1"
        assert resp.source_id == "viirs"
        assert resp.output_dir == tmp_path
        assert resp.files == []
        assert resp.harmonised_path is None
        assert resp.plot_path is None
        assert resp.diagnostics is None
        assert resp.error is None

    def test_with_files(self, tmp_path: Path) -> None:
        """Stores a list of output file paths."""
        f1 = tmp_path / "a.tif"
        f2 = tmp_path / "b.tif"
        f1.write_text("")
        f2.write_text("")
        resp = FetchResponse(event_id="E2", source_id="modis", output_dir=tmp_path, files=[f1, f2])
        assert len(resp.files) == 2

    def test_with_harmonised_and_plot(self, tmp_path: Path) -> None:
        """Stores optional harmonised and plot paths."""
        hp = tmp_path / "harm.tif"
        pp = tmp_path / "plot.png"
        hp.write_text("")
        pp.write_text("")
        resp = FetchResponse(
            event_id="E3",
            source_id="gfm",
            output_dir=tmp_path,
            harmonised_path=hp,
            plot_path=pp,
        )
        assert resp.harmonised_path == hp
        assert resp.plot_path == pp

    def test_error_state(self, tmp_path: Path) -> None:
        """Error field indicates failure."""
        resp = FetchResponse(
            event_id="fail",
            source_id="viirs",
            output_dir=tmp_path,
            error="Network unreachable",
        )
        assert resp.error == "Network unreachable"
        assert resp.files == []

    def test_with_diagnostics(self, tmp_path: Path) -> None:
        """diagnostics field stores source-specific info."""
        diag = type("D", (), {"no_items_found": True})()
        resp = FetchResponse(
            event_id="E4",
            source_id="viirs",
            output_dir=tmp_path,
            diagnostics=diag,
        )
        assert resp.diagnostics.no_items_found is True


class TestEventSummary:
    """Tests for EventSummary history-card container."""

    def test_creation(self, tmp_path: Path) -> None:
        """All fields are stored as given."""
        s = EventSummary(
            event_id="Valencia_2024",
            sources=["viirs", "gfm"],
            file_count=12,
            dates=["2024-10-29", "2024-10-30"],
            root=tmp_path,
        )
        assert s.event_id == "Valencia_2024"
        assert s.sources == ["viirs", "gfm"]
        assert s.file_count == 12
        assert len(s.dates) == 2
        assert s.root == tmp_path

    def test_empty_dates(self, tmp_path: Path) -> None:
        """dates can be empty if no date tokens were extracted."""
        s = EventSummary(
            event_id="Empty",
            sources=["viirs"],
            file_count=0,
            dates=[],
            root=tmp_path,
        )
        assert s.dates == []

    def test_multiple_sources(self, tmp_path: Path) -> None:
        """sources list preserves insertion order."""
        s = EventSummary(
            event_id="Multi",
            sources=["gfm", "modis", "viirs"],
            file_count=90,
            dates=["2024-01-01"],
            root=tmp_path,
        )
        assert s.sources == ["gfm", "modis", "viirs"]
