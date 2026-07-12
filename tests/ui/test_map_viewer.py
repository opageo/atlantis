"""Tests for map viewer components."""

from __future__ import annotations

import numpy as np
import pytest

from atlantis.ui.components.map_viewer import flood_map_plotly, plotly_legend_from_codes

# ---------------------------------------------------------------------------
# Try the optional import to decide whether Plotly-dependent tests should run
# ---------------------------------------------------------------------------
try:
    import plotly.graph_objects as go  # noqa: F401

    PLOTLY_AVAILABLE = True
except ImportError:
    PLOTLY_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PLOTLY_AVAILABLE, reason="plotly not installed")


class TestPlotlyLegendFromCodes:
    """Tests for building legend trace dicts from pixel-code mappings."""

    def test_single_entry(self) -> None:
        """Single code → single legend entry."""
        codes = {1: ("Water", "#0000FF")}
        result = plotly_legend_from_codes(codes)
        assert result == [{"code": 1, "label": "Water", "color": "#0000FF"}]

    def test_sorted_by_code(self) -> None:
        """Entries are returned sorted by code value."""
        codes = {3: ("Cloud", "#808080"), 1: ("Water", "#0000FF"), 2: ("Land", "#00FF00")}
        result = plotly_legend_from_codes(codes)
        assert [e["code"] for e in result] == [1, 2, 3]

    def test_empty_codes(self) -> None:
        """Empty dict returns empty list."""
        assert plotly_legend_from_codes({}) == []


class TestFloodMapPlotly:
    """Tests for Plotly flood map rendering."""

    def test_no_input_returns_none(self) -> None:
        """No geotiff_path and no data_array → None."""
        assert flood_map_plotly() is None

    def test_classified_from_array(self) -> None:
        """Renders a classified (continuous) map from a numpy array."""
        arr = np.array([[0.0, 0.5], [1.0, 0.0]], dtype=np.float64)
        fig = flood_map_plotly(data_array=arr, title="Test", is_classified=True)
        assert fig is not None
        assert fig.layout.title.text == "Test"

    def test_unclassified_from_array(self) -> None:
        """Renders an unclassified (categorical) map from a numpy array."""
        arr = np.array([[1.0, 2.0], [3.0, np.nan]], dtype=np.float64)
        fig = flood_map_plotly(data_array=arr, is_classified=False, title="Raw")
        assert fig is not None
        assert fig.layout.title.text == "Raw"

    def test_classified_default(self) -> None:
        """is_classified defaults to True."""
        arr = np.ones((3, 3), dtype=np.float64)
        fig = flood_map_plotly(data_array=arr)
        assert fig is not None

    def test_integer_array_classified(self) -> None:
        """Integer array with classified mode renders correctly."""
        arr = np.array([[0, 1], [2, 1]], dtype=np.int32)
        fig = flood_map_plotly(data_array=arr, is_classified=True)
        assert fig is not None

    def test_integer_array_unclassified(self) -> None:
        """Integer array with unclassified mode renders correctly."""
        arr = np.array([[0, 1], [2, 1]], dtype=np.int32)
        fig = flood_map_plotly(data_array=arr, is_classified=False)
        assert fig is not None

    def test_all_nan_array(self) -> None:
        """Array of all NaN values should not crash."""
        arr = np.full((2, 2), np.nan, dtype=np.float64)
        fig = flood_map_plotly(data_array=arr, is_classified=True)
        assert fig is not None

    def test_layout_axes_labels(self) -> None:
        """Axes are labelled Longitude and Latitude."""
        arr = np.eye(5, dtype=np.float64)
        fig = flood_map_plotly(data_array=arr)
        assert fig is not None
        assert fig.layout.xaxis.title.text == "Longitude"
        assert fig.layout.yaxis.title.text == "Latitude"

    def test_returns_none_for_bad_geotiff(self, tmp_path) -> None:
        """A non-GeoTIFF file path returns None gracefully."""
        bad = tmp_path / "not_a_geotiff.tif"
        bad.write_text("not a geotiff")
        result = flood_map_plotly(geotiff_path=bad)
        assert result is None

    def test_title_appears(self) -> None:
        """Custom title is set on the figure."""
        arr = np.zeros((2, 2), dtype=np.float64)
        fig = flood_map_plotly(data_array=arr, title="Flood Map — Valencia")
        assert fig is not None
        assert fig.layout.title.text == "Flood Map — Valencia"
