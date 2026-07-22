"""Unit tests for atlantis.fetchers.gfm.catalog (EODC STAC -> Parquet inventory)."""

from __future__ import annotations

from datetime import date as _date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from atlantis.fetchers.gfm.catalog import _search_day, build_catalog

_ITEM_HREF = "https://stac.eodc.eu/api/v1/collections/GFM/items/{}"


def _fake_item(item_id: str, tile: str | None, bbox: tuple | None):
    return SimpleNamespace(
        id=item_id,
        properties={"Equi7Tile": tile} if tile is not None else {},
        bbox=bbox,
        self_href=_ITEM_HREF.format(item_id),
    )


def _patched_search(items):
    """Context manager patching pystac_client.Client.open to return *items*."""
    mock_search = MagicMock()
    mock_search.items.return_value = iter(items)
    mock_catalog = MagicMock()
    mock_catalog.search.return_value = mock_search
    return patch("pystac_client.Client.open", return_value=mock_catalog)


class TestSearchDay:
    @patch("atlantis.fetchers.gfm.catalog.retry_request")
    def test_parses_items_into_rows(self, mock_retry):
        mock_retry.side_effect = lambda fn, **kw: fn()
        items = [
            _fake_item("a", "EU020M_E036N009T3", (1.0, 2.0, 3.0, 4.0)),
            _fake_item("b", "EU020M_E036N006T3", (5.0, 6.0, 7.0, 8.0)),
        ]
        with _patched_search(items):
            rows = _search_day("https://stac.eodc.eu/api/v1", _date(2024, 11, 1))

        assert len(rows) == 2
        assert rows[0] == {
            "date": "2024-11-01",
            "equi7_tile": "EU020M_E036N009T3",
            "item_id": "a",
            "item_href": _ITEM_HREF.format("a"),
            "west": 1.0,
            "south": 2.0,
            "east": 3.0,
            "north": 4.0,
        }

    @patch("atlantis.fetchers.gfm.catalog.retry_request")
    def test_skips_items_without_tile(self, mock_retry):
        mock_retry.side_effect = lambda fn, **kw: fn()
        items = [_fake_item("a", None, (1.0, 2.0, 3.0, 4.0))]
        with _patched_search(items):
            rows = _search_day("https://stac.eodc.eu/api/v1", _date(2024, 11, 1))
        assert rows == []

    @patch("atlantis.fetchers.gfm.catalog.retry_request")
    def test_skips_items_without_bbox(self, mock_retry):
        mock_retry.side_effect = lambda fn, **kw: fn()
        items = [_fake_item("a", "EU020M_E036N009T3", None)]
        with _patched_search(items):
            rows = _search_day("https://stac.eodc.eu/api/v1", _date(2024, 11, 1))
        assert rows == []


class TestBuildCatalog:
    _ROW = {
        "date": "2024-11-01",
        "equi7_tile": "EU020M_E036N009T3",
        "item_id": "a",
        "item_href": _ITEM_HREF.format("a"),
        "west": 1.0,
        "south": 2.0,
        "east": 3.0,
        "north": 4.0,
    }

    @patch("atlantis.fetchers.gfm.catalog._search_day")
    def test_builds_catalog_and_writes_parquet(self, mock_search, tmp_path):
        mock_search.return_value = [self._ROW]
        output = tmp_path / "catalog.parquet"
        result = build_catalog("2024-11-01", "2024-11-01", output)

        assert result == output
        assert output.exists()
        df = pd.read_parquet(output)
        assert len(df) == 1
        assert list(df.columns) == [
            "date",
            "equi7_tile",
            "item_id",
            "item_href",
            "west",
            "south",
            "east",
            "north",
        ]

    @patch("atlantis.fetchers.gfm.catalog._search_day")
    def test_raises_when_no_items_found(self, mock_search, tmp_path):
        mock_search.return_value = []
        with pytest.raises(RuntimeError, match="No GFM items found"):
            build_catalog("2024-11-01", "2024-11-01", tmp_path / "catalog.parquet")

    @patch("atlantis.fetchers.gfm.catalog._search_day")
    def test_aborts_on_persistent_day_failure(self, mock_search, tmp_path):
        """A day that still fails after retries must abort the whole build, not silently truncate it."""
        mock_search.side_effect = [RuntimeError("network error"), [self._ROW]]
        output = tmp_path / "catalog.parquet"
        with pytest.raises(RuntimeError, match="GFM catalog build aborted.*2024-11-01"):
            build_catalog("2024-11-01", "2024-11-02", output)
        assert not output.exists()

    @patch("atlantis.fetchers.gfm.catalog._search_day")
    def test_progress_callback_invoked(self, mock_search, tmp_path):
        mock_search.return_value = [self._ROW]
        messages: list[str] = []
        build_catalog(
            "2024-11-01",
            "2024-11-01",
            tmp_path / "catalog.parquet",
            on_progress=messages.append,
        )
        assert any("GFM catalog" in m for m in messages)
