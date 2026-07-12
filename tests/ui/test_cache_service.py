"""Tests for the cache service layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlantis.ui.services.cache_service import (
    _extract_date_token,
    _get_cache_dir,
    find_harmonised,
    find_plot,
    list_events,
    list_files,
)


class TestExtractDateToken:
    """Tests for the YYYYMMDD regex extractor."""

    def test_extracts_yyyymmdd(self) -> None:
        """8 consecutive digits form a date token."""
        assert _extract_date_token("Valencia_20241029") == "2024-10-29"

    def test_extracts_from_middle(self) -> None:
        """Date can appear anywhere in the stem."""
        assert _extract_date_token("abc_20220101_def") == "2022-01-01"

    def test_returns_none_for_no_date(self) -> None:
        """No 8-digit sequence → None."""
        assert _extract_date_token("hello_world") is None

    def test_returns_none_for_short_digit_run(self) -> None:
        """Fewer than 8 digits is ignored."""
        assert _extract_date_token("ev_2024102") is None

    def test_first_match_used(self) -> None:
        """Only the first 8-digit run is extracted."""
        assert _extract_date_token("x_20240101_y_20250101") == "2024-01-01"

    def test_invalid_month_day_still_extracted(self) -> None:
        """Regex is lenient; `_extract_date_token` returns any 8-digit run."""
        assert _extract_date_token("data_99999999") == "9999-99-99"


class TestGetCacheDir:
    """Tests for the configured cache directory resolver."""

    def test_returns_path(self) -> None:
        """Returns a Path from the application config."""
        result = _get_cache_dir()
        assert isinstance(result, Path)

    def test_is_directory(self) -> None:
        """The resolved path should be a real directory on disk."""
        result = _get_cache_dir()
        assert isinstance(result, Path)
        assert result.exists()


class TestListEvents:
    """Tests for cache directory scanning."""

    def test_empty_cache_returns_empty(self, tmp_path: Path) -> None:
        """No raw/ dir means no events."""
        cache = tmp_path / "cache"
        assert list_events(cache_dir=cache) == []

    def test_empty_raw_dir(self, tmp_path: Path) -> None:
        """Empty raw/ dir yields empty list."""
        raw = tmp_path / "cache" / "raw"
        raw.mkdir(parents=True)
        assert list_events(cache_dir=tmp_path / "cache") == []

    def test_files_ignored_at_event_level(self, tmp_path: Path) -> None:
        """Files (not dirs) in raw/ are skipped."""
        raw = tmp_path / "cache" / "raw"
        raw.mkdir(parents=True)
        (raw / "orphan.txt").write_text("")
        assert list_events(cache_dir=tmp_path / "cache") == []

    def test_single_event_with_source_files(self, tmp_path: Path) -> None:
        """One event with source subdirs containing files."""
        raw = tmp_path / "cache" / "raw"
        event = raw / "Valencia_2024"
        viirs = event / "viirs"
        viirs.mkdir(parents=True)
        (viirs / "VNP09_NRT_20241029.tif").write_text("")
        (viirs / "VNP09_NRT_20241030.tif").write_text("")

        summaries = list_events(cache_dir=tmp_path / "cache")
        assert len(summaries) == 1
        s = summaries[0]
        assert s.event_id == "Valencia_2024"
        assert s.sources == ["viirs"]
        assert s.file_count == 2
        assert s.dates == ["2024-10-29", "2024-10-30"]

    def test_multiple_sources(self, tmp_path: Path) -> None:
        """Event with multiple source subdirectories."""
        raw = tmp_path / "cache" / "raw"
        event = raw / "MultiSource"
        for src in ("viirs", "modis"):
            d = event / src
            d.mkdir(parents=True)
            (d / f"{src}_20240101.tif").write_text("")

        summaries = list_events(cache_dir=tmp_path / "cache")
        assert len(summaries) == 1
        s = summaries[0]
        assert sorted(s.sources) == ["modis", "viirs"]
        assert s.file_count == 2

    def test_multiple_events(self, tmp_path: Path) -> None:
        """Events are returned newest first (reverse alphabetical)."""
        raw = tmp_path / "cache" / "raw"
        for name in ("Event_A", "Event_B", "Event_C"):
            d = raw / name / "viirs"
            d.mkdir(parents=True)
            (d / "file.tif").write_text("")

        summaries = list_events(cache_dir=tmp_path / "cache")
        assert len(summaries) == 3
        assert [s.event_id for s in summaries] == ["Event_C", "Event_B", "Event_A"]

    def test_events_without_source_dirs_ignored(self, tmp_path: Path) -> None:
        """An event directory with no sub-source dirs is skipped."""
        raw = tmp_path / "cache" / "raw"
        (raw / "EmptyEvent").mkdir(parents=True)
        assert list_events(cache_dir=tmp_path / "cache") == []

    def test_nested_files_in_source(self, tmp_path: Path) -> None:
        """Files in nested dirs within a source are counted."""
        raw = tmp_path / "cache" / "raw"
        gfm = raw / "NestedEvent" / "gfm" / "harmonised"
        gfm.mkdir(parents=True)
        (gfm / "harm.tif").write_text("")

        summaries = list_events(cache_dir=tmp_path / "cache")
        assert len(summaries) == 1
        assert summaries[0].file_count == 1

    def test_non_dir_item_at_event_level_skipped(self, tmp_path: Path) -> None:
        """Files (not directories) at the event level (not inside source dirs) are ignored."""
        raw = tmp_path / "cache" / "raw"
        event = raw / "EventWithFile"
        viirs = event / "viirs"
        viirs.mkdir(parents=True)
        (viirs / "a.tif").write_text("")
        (event / "readme.txt").write_text("")

        summaries = list_events(cache_dir=tmp_path / "cache")
        assert len(summaries) == 1
        assert summaries[0].file_count == 1


class TestListFiles:
    """Tests for listing files under an event root."""

    def test_no_source_filter(self, tmp_path: Path) -> None:
        """Without a source filter, all files under root are returned."""
        (tmp_path / "viirs").mkdir(parents=True)
        (tmp_path / "viirs" / "a.tif").write_text("")
        (tmp_path / "modis").mkdir(parents=True)
        (tmp_path / "modis" / "b.tif").write_text("")

        files = list_files(tmp_path)
        assert len(files) == 2
        names = {f.name for f in files}
        assert names == {"a.tif", "b.tif"}

    def test_with_source_filter(self, tmp_path: Path) -> None:
        """Source filter restricts to that subdirectory."""
        (tmp_path / "viirs").mkdir(parents=True)
        (tmp_path / "viirs" / "a.tif").write_text("")
        (tmp_path / "modis").mkdir(parents=True)
        (tmp_path / "modis" / "b.tif").write_text("")

        files = list_files(tmp_path, source="viirs")
        assert len(files) == 1
        assert files[0].name == "a.tif"

    def test_non_existent_dir_returns_empty(self, tmp_path: Path) -> None:
        """Missing directory yields empty list."""
        assert list_files(tmp_path / "no_dir") == []

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        """Directory with no files yields empty list."""
        (tmp_path / "empty").mkdir()
        assert list_files(tmp_path / "empty") == []


class TestFindHarmonised:
    """Tests for harmonised GeoTIFF discovery."""

    def test_finds_harmonised(self, tmp_path: Path) -> None:
        """Returns path to first .tif in harmonised/ dir."""
        harm = tmp_path / "viirs" / "harmonised"
        harm.mkdir(parents=True)
        tif = harm / "event_harm.tif"
        tif.write_text("")

        assert find_harmonised(tmp_path, "viirs") == tif

    def test_no_harmonised_dir_returns_none(self, tmp_path: Path) -> None:
        """When harmonised/ doesn't exist, returns None."""
        assert find_harmonised(tmp_path, "viirs") is None

    def test_empty_harmonised_dir_returns_none(self, tmp_path: Path) -> None:
        """When harmonised/ has no .tif files, returns None."""
        (tmp_path / "viirs" / "harmonised").mkdir(parents=True)
        assert find_harmonised(tmp_path, "viirs") is None


class TestFindPlot:
    """Tests for plot PNG discovery."""

    def test_finds_plot_in_source_dir(self, tmp_path: Path) -> None:
        """Returns path to PNG in plots/ subdir of source."""
        plot_dir = tmp_path / "viirs" / "plots"
        plot_dir.mkdir(parents=True)
        png = plot_dir / "viirs_20241029.png"
        png.write_text("")

        assert find_plot(tmp_path, "viirs") == png

    def test_finds_plot_in_event_root_plots(self, tmp_path: Path) -> None:
        """Falls back to event-root plots/ dir."""
        plot_dir = tmp_path / "plots"
        plot_dir.mkdir(parents=True)
        png = plot_dir / "viirs_20241029.png"
        png.write_text("")

        assert find_plot(tmp_path, "viirs") == png

    def test_no_plots_returns_none(self, tmp_path: Path) -> None:
        """No plots directory → None."""
        assert find_plot(tmp_path, "viirs") is None

    def test_empty_plots_returns_none(self, tmp_path: Path) -> None:
        """Plots directory with no matching PNGs → None."""
        (tmp_path / "viirs" / "plots").mkdir(parents=True)
        assert find_plot(tmp_path, "viirs") is None

    def test_prefers_matching_source_name(self, tmp_path: Path) -> None:
        """A PNG whose name contains the source id is preferred."""
        plot_dir = tmp_path / "viirs" / "plots"
        plot_dir.mkdir(parents=True)
        (plot_dir / "generic.png").write_text("")
        pref = plot_dir / "viirs_best.png"
        pref.write_text("")
        assert find_plot(tmp_path, "viirs") == pref

    def test_falls_back_to_first_png_when_no_source_match(self, tmp_path: Path) -> None:
        """When no PNG name contains the source id, first PNG found is returned."""
        plot_dir = tmp_path / "modis" / "plots"
        plot_dir.mkdir(parents=True)
        png1 = plot_dir / "some_plot.png"
        png2 = plot_dir / "another.png"
        png1.write_text("")
        png2.write_text("")
        assert find_plot(tmp_path, "modis") in (png1, png2)
