"""Tests for the GFM fetcher."""

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult
from atlantis.fetchers.gfm import GFMFetcher
from atlantis.fetchers.gfm.backend import DEFAULT_GFM_STAC_URL, GFM_COLLECTION_ID, GfmStacBackend
from atlantis.fetchers.gfm.processor import (
    DEFAULT_COARSEN_FACTOR,
    GFM_DRY,
    GFM_FLOOD,
    GFM_NODATA,
    GFM_PERMANENT_WATER,
    GfmProcessedTile,
    GfmRasterProcessor,
)
from atlantis.fetchers.gfm.selection import flood_pixel_count
from atlantis.fetchers.registry import fetcher_registry
from atlantis.models.event import FloodEvent

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def valencia_event():
    """Standard test event: Valencia 2024 flood."""
    return FloodEvent(
        event_id="valencia_2024",
        bbox=(-2.5, 37.0, 1.5, 41.0),
        start_date=date(2024, 10, 30),
        end_date=date(2024, 10, 31),
        sources=["gfm"],
    )


@pytest.fixture
def small_event():
    """Small event for quick tests."""
    return FloodEvent(
        event_id="test_small",
        bbox=(10.0, 20.0, 11.0, 21.0),
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 1),
        sources=["gfm"],
    )


def _make_processed_tile(shape=(100, 100), flood_frac=0.3, perm_frac=0.1) -> GfmProcessedTile:
    """Create a synthetic GfmProcessedTile for testing."""
    from rasterio.transform import from_bounds

    h, w = shape
    rng = np.random.default_rng(42)
    flood_fraction = rng.random((h, w), dtype=np.float32) * flood_frac
    quality_mask = np.ones((h, w), dtype=np.uint8)
    permanent_water = (rng.random((h, w)) < perm_frac).astype(np.uint8)
    transform = from_bounds(10.0, 20.0, 11.0, 21.0, w, h)

    return GfmProcessedTile(
        flood_fraction=flood_fraction,
        quality_mask=quality_mask,
        permanent_water=permanent_water,
        transform=transform,
        crs="EPSG:4326",
        shape=shape,
        cloud_fraction=0.05,
    )


def _make_mock_stac_item(item_id="test_item_001", dt=None, bbox=None):
    """Create a mock STAC item."""
    item = MagicMock()
    item.id = item_id
    item.datetime = dt or datetime(2024, 10, 30, 6, 0, 0, tzinfo=timezone.utc)
    item.bbox = bbox or [10.0, 20.0, 11.0, 21.0]
    item.self_href = f"https://stac.eodc.eu/api/v1/collections/GFM/items/{item_id}"
    item.properties = {
        "proj:wkt2": 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
        "gsd": 10,
        "datetime": "2024-10-30T06:00:00Z",
    }
    return item


# ── Structure Tests ───────────────────────────────────────────────────────────


class TestGFMFetcherStructure:
    """Verify GFMFetcher satisfies the fetcher protocol/ABC contract."""

    def test_is_abstract_flood_fetcher(self):
        assert issubclass(GFMFetcher, AbstractFloodFetcher)

    def test_has_source_id(self):
        assert GFMFetcher.source_id == "gfm"

    def test_instantiation_default(self):
        from rasterio.enums import Resampling

        fetcher = GFMFetcher()
        assert fetcher.api_url == DEFAULT_GFM_STAC_URL
        assert fetcher.coarsen_factor == DEFAULT_COARSEN_FACTOR
        assert fetcher.resampling == Resampling.average
        assert fetcher.strategy == "aggregate"
        assert fetcher.keep_processed is True

    def test_instantiation_custom(self):
        fetcher = GFMFetcher(
            api_url="https://custom.api/v1",
            coarsen_factor=2,
            resampling="nearest",
            strategy="peak",
            keep_processed=False,
        )
        assert fetcher.api_url == "https://custom.api/v1"
        assert fetcher.coarsen_factor == 2
        assert fetcher.resampling == "nearest"
        assert fetcher.strategy == "peak"
        assert fetcher.keep_processed is False

    def test_registered_in_registry(self):
        assert "gfm" in fetcher_registry

    def test_is_protocol_compliant(self):
        fetcher = GFMFetcher()
        assert callable(fetcher.search)
        assert callable(fetcher.fetch)
        assert callable(fetcher.to_dataset)


# ── Backend Tests ─────────────────────────────────────────────────────────────


class TestGfmStacBackend:
    def test_default_config(self):
        backend = GfmStacBackend()
        assert backend.api_url == DEFAULT_GFM_STAC_URL
        assert backend.collection_id == GFM_COLLECTION_ID
        assert backend.max_items == 1000

    def test_custom_config(self):
        backend = GfmStacBackend(
            api_url="https://custom.api/v1",
            collection_id="CUSTOM",
            max_items=500,
        )
        assert backend.api_url == "https://custom.api/v1"
        assert backend.collection_id == "CUSTOM"
        assert backend.max_items == 500

    @patch("pystac_client.Client")
    def test_search_calls_stac_client(self, mock_client_cls, valencia_event):
        mock_catalog = MagicMock()
        mock_client_cls.open.return_value = mock_catalog
        mock_search = MagicMock()
        mock_catalog.search.return_value = mock_search
        mock_search.item_collection.return_value = []

        backend = GfmStacBackend()
        result = backend.search(valencia_event)

        mock_client_cls.open.assert_called_once_with(DEFAULT_GFM_STAC_URL)
        mock_catalog.search.assert_called_once()
        call_kwargs = mock_catalog.search.call_args[1]
        assert call_kwargs["max_items"] == 1000
        assert call_kwargs["collections"] == GFM_COLLECTION_ID
        assert result == []

    def test_group_items_by_date(self):
        item1 = _make_mock_stac_item("item1", datetime(2024, 10, 30, 6, 0, tzinfo=timezone.utc))
        item2 = _make_mock_stac_item("item2", datetime(2024, 10, 30, 18, 0, tzinfo=timezone.utc))
        item3 = _make_mock_stac_item("item3", datetime(2024, 10, 31, 6, 0, tzinfo=timezone.utc))

        groups = GfmStacBackend.group_items_by_date([item1, item2, item3])
        assert "20241030" in groups
        assert "20241031" in groups
        assert len(groups["20241030"]) == 2
        assert len(groups["20241031"]) == 1

    def test_group_items_by_date_no_datetime(self):
        item = _make_mock_stac_item("item_no_dt")
        item.datetime = None
        item.properties = {"datetime": ""}

        groups = GfmStacBackend.group_items_by_date([item])
        assert groups == {}


# ── Processor Tests ───────────────────────────────────────────────────────────


class TestGfmRasterProcessor:
    def test_init(self):
        from rasterio.enums import Resampling

        proc = GfmRasterProcessor(bbox=(10.0, 20.0, 11.0, 21.0))
        assert proc.bbox == (10.0, 20.0, 11.0, 21.0)
        assert proc.coarsen_factor == DEFAULT_COARSEN_FACTOR
        assert proc.resampling == Resampling.average

    def test_init_custom(self):
        from rasterio.enums import Resampling

        proc = GfmRasterProcessor(bbox=(0, 0, 1, 1), coarsen_factor=2, resampling=Resampling.nearest)
        assert proc.coarsen_factor == 2
        assert proc.resampling == Resampling.nearest

    def test_process_items_empty(self):
        proc = GfmRasterProcessor(bbox=(10.0, 20.0, 11.0, 21.0))
        result = proc.process_items([], event_id="test")
        assert result is None

    def test_flood_pixel_count(self):
        tile = _make_processed_tile(shape=(10, 10), flood_frac=0.5)
        count = flood_pixel_count(tile)
        assert count > 0
        assert count <= 100

    def test_flood_pixel_count_zero(self):
        from rasterio.transform import from_bounds

        tile = GfmProcessedTile(
            flood_fraction=np.zeros((10, 10), dtype=np.float32),
            quality_mask=np.ones((10, 10), dtype=np.uint8),
            permanent_water=np.zeros((10, 10), dtype=np.uint8),
            transform=from_bounds(0, 0, 1, 1, 10, 10),
            crs="EPSG:4326",
            shape=(10, 10),
        )
        assert flood_pixel_count(tile) == 0

    def test_aggregate_tiles_single(self):
        tile = _make_processed_tile()
        result = GfmRasterProcessor.aggregate_tiles([tile])
        assert result is tile

    def test_aggregate_tiles_empty(self):
        result = GfmRasterProcessor.aggregate_tiles([])
        assert result is None

    def test_aggregate_tiles_multiple(self):
        tile1 = _make_processed_tile(shape=(10, 10), flood_frac=0.2)
        tile2 = _make_processed_tile(shape=(10, 10), flood_frac=0.6)
        result = GfmRasterProcessor.aggregate_tiles([tile1, tile2])
        assert result is not None
        assert result.flood_fraction.shape == (10, 10)
        assert result.quality_mask.shape == (10, 10)
        assert result.permanent_water.shape == (10, 10)
        assert result.crs == "EPSG:4326"


# ── Search Tests ──────────────────────────────────────────────────────────────


class TestGFMFetcherSearch:
    @patch("pystac_client.Client")
    def test_search_returns_search_results(self, mock_client_cls, small_event):
        mock_catalog = MagicMock()
        mock_client_cls.open.return_value = mock_catalog
        mock_search = MagicMock()
        mock_catalog.search.return_value = mock_search

        items = [
            _make_mock_stac_item("item1", datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc)),
            _make_mock_stac_item("item2", datetime(2024, 1, 1, 18, 0, tzinfo=timezone.utc)),
        ]
        mock_search.item_collection.return_value = items

        fetcher = GFMFetcher()
        results = fetcher.search(small_event)

        assert len(results) == 2
        assert results[0].source_id == "gfm"
        assert results[0].item_id == "item1"
        assert results[1].item_id == "item2"

    @patch("pystac_client.Client")
    def test_search_empty(self, mock_client_cls, small_event):
        mock_catalog = MagicMock()
        mock_client_cls.open.return_value = mock_catalog
        mock_search = MagicMock()
        mock_catalog.search.return_value = mock_search
        mock_search.item_collection.return_value = []

        fetcher = GFMFetcher()
        results = fetcher.search(small_event)
        assert results == []


# ── Fetch Tests ───────────────────────────────────────────────────────────────


class TestGFMFetcherFetch:
    @patch("pystac_client.Client")
    def test_fetch_empty_search(self, mock_client_cls, small_event, tmp_path):
        mock_catalog = MagicMock()
        mock_client_cls.open.return_value = mock_catalog
        mock_search = MagicMock()
        mock_catalog.search.return_value = mock_search
        mock_search.item_collection.return_value = []

        fetcher = GFMFetcher()
        results = fetcher.fetch(small_event, tmp_path)
        assert results == []

    def test_fetch_with_mock_processor(self, small_event, tmp_path):
        """Test fetch with mocked backend and processor."""
        fetcher = GFMFetcher(strategy="aggregate")

        tile = _make_processed_tile(shape=(50, 50))
        mock_result = MagicMock()
        mock_result.processed = tile

        items = [
            _make_mock_stac_item("item1", datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc)),
        ]

        with patch.object(fetcher._backend, "search", return_value=items):
            with patch.object(fetcher._backend, "group_items_by_date", return_value={"20240101": items}):
                with patch(
                    "atlantis.fetchers.gfm.GfmRasterProcessor.process_items",
                    return_value=mock_result,
                ):
                    results = fetcher.fetch(small_event, tmp_path)

        assert len(results) == 1
        assert results[0].source_id == "gfm"
        assert results[0].event_id == "test_small"
        assert results[0].dataset is not None

    def test_fetch_peak_strategy(self, small_event, tmp_path):
        """Test that peak strategy selects the date with most flood pixels."""
        fetcher = GFMFetcher(strategy="peak")

        # tile_low has no flood pixels, tile_high has many
        from rasterio.transform import from_bounds

        tile_low = GfmProcessedTile(
            flood_fraction=np.zeros((10, 10), dtype=np.float32),
            quality_mask=np.ones((10, 10), dtype=np.uint8),
            permanent_water=np.zeros((10, 10), dtype=np.uint8),
            transform=from_bounds(10, 20, 11, 21, 10, 10),
            crs="EPSG:4326",
            shape=(10, 10),
        )
        tile_high = GfmProcessedTile(
            flood_fraction=np.full((10, 10), 0.8, dtype=np.float32),
            quality_mask=np.ones((10, 10), dtype=np.uint8),
            permanent_water=np.zeros((10, 10), dtype=np.uint8),
            transform=from_bounds(10, 20, 11, 21, 10, 10),
            crs="EPSG:4326",
            shape=(10, 10),
        )

        items = [_make_mock_stac_item("item1"), _make_mock_stac_item("item2")]
        date_groups = {"20240101": [items[0]], "20240102": [items[1]]}

        mock_result_low = MagicMock()
        mock_result_low.processed = tile_low
        mock_result_high = MagicMock()
        mock_result_high.processed = tile_high

        def mock_process(items_arg, **kwargs):
            date_token = kwargs.get("date_token", "")
            if date_token == "20240101":
                return mock_result_low
            return mock_result_high

        with patch.object(fetcher._backend, "search", return_value=items):
            with patch.object(fetcher._backend, "group_items_by_date", return_value=date_groups):
                with patch(
                    "atlantis.fetchers.gfm.GfmRasterProcessor.process_items",
                    side_effect=mock_process,
                ):
                    results = fetcher.fetch(small_event, tmp_path)

        assert len(results) == 1
        assert results[0].date_token == "20240102"

    def test_fetch_all_strategy(self, small_event, tmp_path):
        """Test that all strategy returns one result per date."""
        fetcher = GFMFetcher(strategy="all")

        tile = _make_processed_tile(shape=(10, 10))
        items = [_make_mock_stac_item("item1"), _make_mock_stac_item("item2")]
        date_groups = {"20240101": [items[0]], "20240102": [items[1]]}

        mock_result = MagicMock()
        mock_result.processed = tile

        with patch.object(fetcher._backend, "search", return_value=items):
            with patch.object(fetcher._backend, "group_items_by_date", return_value=date_groups):
                with patch(
                    "atlantis.fetchers.gfm.GfmRasterProcessor.process_items",
                    return_value=mock_result,
                ):
                    results = fetcher.fetch(small_event, tmp_path)

        assert len(results) == 2
        date_tokens = {r.date_token for r in results}
        assert "20240101" in date_tokens
        assert "20240102" in date_tokens


# ── ToDataset Tests ───────────────────────────────────────────────────────────


class TestGFMFetcherToDataset:
    def test_to_dataset_from_in_memory(self):
        import xarray as xr

        fetcher = GFMFetcher()
        ds = xr.Dataset({"flood_fraction": xr.DataArray(np.zeros((5, 5)))})
        from atlantis.models.metadata import TileMetadata

        result = FetchResult(
            event_id="test",
            source_id="gfm",
            files=[],
            metadata=TileMetadata(
                event_id="test",
                source_id="gfm",
                fetch_timestamp=datetime.now(timezone.utc),
                bbox=(0, 0, 1, 1),
            ),
            dataset=ds,
        )

        out = fetcher.to_dataset(result)
        assert "flood_fraction" in out.data_vars

    def test_to_dataset_no_data_raises(self):
        fetcher = GFMFetcher()
        from atlantis.models.metadata import TileMetadata

        result = FetchResult(
            event_id="test",
            source_id="gfm",
            files=[],
            metadata=TileMetadata(
                event_id="test",
                source_id="gfm",
                fetch_timestamp=datetime.now(timezone.utc),
                bbox=(0, 0, 1, 1),
            ),
            dataset=None,
        )

        with pytest.raises(ValueError, match="neither in-memory dataset nor files"):
            fetcher.to_dataset(result)
