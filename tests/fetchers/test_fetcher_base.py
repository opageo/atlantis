"""Tests for the base fetcher module (ABC, protocols, dataclasses)."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from atlantis.fetchers.base import (
    AbstractFloodFetcher,
    FetchResult,
    FloodFetcher,
    SearchResult,
)
from atlantis.models.event import FloodEvent
from atlantis.models.metadata import TileMetadata


class TestSearchResult:
    """Tests for SearchResult dataclass."""

    def test_minimal_construction(self) -> None:
        """Test SearchResult with only required fields."""
        ts = datetime(2024, 10, 31, tzinfo=timezone.utc)
        result = SearchResult(
            source_id="viirs",
            item_id="VNP46A1_20241031",
            timestamp=ts,
            bbox=(0.0, 0.0, 1.0, 1.0),
        )
        assert result.source_id == "viirs"
        assert result.item_id == "VNP46A1_20241031"
        assert result.timestamp == ts
        assert result.bbox == (0.0, 0.0, 1.0, 1.0)
        assert result.cloud_fraction == 0.0
        assert result.url == ""
        assert result.properties == {}

    def test_full_construction(self) -> None:
        """Test SearchResult with all fields."""
        ts = datetime(2024, 10, 31, tzinfo=timezone.utc)
        result = SearchResult(
            source_id="viirs",
            item_id="VNP46A1_20241031",
            timestamp=ts,
            bbox=(0.0, 0.0, 1.0, 1.0),
            cloud_fraction=0.3,
            url="https://example.com/data.tif",
            properties={"resolution": "375m", "satellite": "NPP"},
        )
        assert result.cloud_fraction == 0.3
        assert result.url == "https://example.com/data.tif"
        assert result.properties["resolution"] == "375m"

    def test_default_properties_isolation(self) -> None:
        """Test that default properties dict doesn't share state."""
        r1 = SearchResult(source_id="viirs", item_id="a", timestamp=datetime.now(timezone.utc), bbox=(0, 0, 1, 1))
        r2 = SearchResult(source_id="viirs", item_id="b", timestamp=datetime.now(timezone.utc), bbox=(0, 0, 1, 1))
        r1.properties["custom"] = "value1"
        assert "custom" not in r2.properties


class TestFetchResult:
    """Tests for FetchResult dataclass."""

    def test_minimal_construction(self) -> None:
        """Test FetchResult with required fields."""
        metadata = TileMetadata(
            event_id="test_event",
            source_id="viirs",
            fetch_timestamp=datetime.now(timezone.utc),
            bbox=(0.0, 0.0, 1.0, 1.0),
        )
        result = FetchResult(
            event_id="test_event",
            source_id="viirs",
            files=[Path("/tmp/test.tif")],
            metadata=metadata,
        )
        assert result.event_id == "test_event"
        assert result.source_id == "viirs"
        assert len(result.files) == 1
        assert isinstance(result.timestamp, datetime)

    def test_timestamp_default_now(self) -> None:
        """Test that timestamp defaults to current UTC time."""
        metadata = TileMetadata(
            event_id="test_event",
            source_id="viirs",
            fetch_timestamp=datetime.now(timezone.utc),
            bbox=(0.0, 0.0, 1.0, 1.0),
        )
        before = datetime.now(timezone.utc)
        result = FetchResult(event_id="e", source_id="s", files=[], metadata=metadata)
        after = datetime.now(timezone.utc)
        assert before <= result.timestamp <= after

    def test_empty_files_list(self) -> None:
        """Test that empty files list is allowed."""
        metadata = TileMetadata(
            event_id="test_event",
            source_id="viirs",
            fetch_timestamp=datetime.now(timezone.utc),
            bbox=(0.0, 0.0, 1.0, 1.0),
        )
        result = FetchResult(event_id="e", source_id="s", files=[], metadata=metadata)
        assert result.files == []


class TestFloodFetcherProtocol:
    """Tests for the FloodFetcher Protocol."""

    def test_is_runtime_checkable(self) -> None:
        """Test that FloodFetcher is runtime checkable via @runtime_checkable."""
        # Verify it's a runtime-checkable Protocol
        assert getattr(FloodFetcher, "_is_protocol", False) is True
        assert getattr(FloodFetcher, "_is_runtime_protocol", False) is True

    def test_protocol_with_duck_typed_class(self) -> None:
        """Test that a class matching the protocol passes isinstance check."""

        class MatchFetcher:
            source_id: str = "test"

            def search(self, event: FloodEvent) -> list[SearchResult]:
                return []

            def fetch(self, event: FloodEvent, output_dir: Path) -> list[FetchResult]:
                return []

        assert isinstance(MatchFetcher(), FloodFetcher)

    def test_protocol_with_incomplete_class(self) -> None:
        """Test that a class missing methods fails isinstance check."""

        class IncompleteFetcher:
            source_id: str = "test"

            def search(self, event: FloodEvent) -> list[SearchResult]:
                return []

            # missing fetch method

        assert not isinstance(IncompleteFetcher(), FloodFetcher)

    def test_protocol_with_missing_source_id(self) -> None:
        """Test that a class missing source_id fails isinstance check."""

        class NoSourceIdFetcher:
            def search(self, event: FloodEvent) -> list[SearchResult]:
                return []

            def fetch(self, event: FloodEvent, output_dir: Path) -> list[FetchResult]:
                return []

        assert not isinstance(NoSourceIdFetcher(), FloodFetcher)


class TestAbstractFloodFetcher:
    """Tests for AbstractFloodFetcher ABC."""

    def test_cannot_instantiate_abstract_class(self) -> None:
        """Test that ABC cannot be instantiated directly."""
        with pytest.raises(TypeError, match="abstract"):
            AbstractFloodFetcher()  # type: ignore[abstract]

    def test_concrete_subclass_enforces_abstract_methods(self) -> None:
        """Test that subclass must implement abstract methods."""
        with pytest.raises(TypeError, match="abstract"):

            class IncompleteFetcher(AbstractFloodFetcher):
                source_id = "test"
                # missing search/fetch

            IncompleteFetcher()  # type: ignore[abstract]

    def test_concrete_subclass_satisfies_protocol(self) -> None:
        """Test that a complete subclass satisfies the FloodFetcher protocol."""

        class CompleteFetcher(AbstractFloodFetcher):
            source_id = "test"

            def search(self, event: FloodEvent) -> list[SearchResult]:
                return []

            def fetch(self, event: FloodEvent, output_dir: Path) -> list[FetchResult]:
                return []

        instance = CompleteFetcher()
        assert isinstance(instance, AbstractFloodFetcher)
        assert isinstance(instance, FloodFetcher)

    def test_to_dataset_default_raises_not_implemented(self) -> None:
        """Test that to_dataset raises NotImplementedError by default."""

        class Fetcher(AbstractFloodFetcher):
            source_id = "test"

            def search(self, event: FloodEvent) -> list[SearchResult]:
                return []

            def fetch(self, event: FloodEvent, output_dir: Path) -> list[FetchResult]:
                return []

        metadata = TileMetadata(
            event_id="test",
            source_id="test",
            fetch_timestamp=datetime.now(timezone.utc),
            bbox=(0, 0, 1, 1),
        )
        result = FetchResult(event_id="test", source_id="test", files=[], metadata=metadata)

        with pytest.raises(NotImplementedError, match="does not implement to_dataset"):
            Fetcher().to_dataset(result)
