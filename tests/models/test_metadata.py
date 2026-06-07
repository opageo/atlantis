"""Tests for metadata models (TileMetadata, SourceMetadata)."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from atlantis.models.metadata import SourceMetadata, TileMetadata


class TestTileMetadata:
    """Tests for TileMetadata pydantic model."""

    def test_minimal_construction(self) -> None:
        """Test TileMetadata with required fields only."""
        ts = datetime(2024, 10, 31, tzinfo=timezone.utc)
        metadata = TileMetadata(
            event_id="valencia_2024",
            source_id="viirs",
            fetch_timestamp=ts,
            bbox=(-1.2, 39.0, 0.2, 39.8),
        )
        assert metadata.event_id == "valencia_2024"
        assert metadata.source_id == "viirs"
        assert metadata.fetch_timestamp == ts
        assert metadata.bbox == (-1.2, 39.0, 0.2, 39.8)
        # Defaults
        assert metadata.crs == "EPSG:4326"
        assert metadata.resolution == 0.0002777777777777778
        assert metadata.cloud_fraction == 0.0
        assert metadata.snow_flag is False
        assert metadata.quality_bitmask == 0
        assert metadata.permanent_water_mask_available is False

    def test_full_construction(self) -> None:
        """Test TileMetadata with all fields."""
        ts = datetime(2024, 10, 31, tzinfo=timezone.utc)
        metadata = TileMetadata(
            event_id="test_event",
            source_id="gfm",
            fetch_timestamp=ts,
            crs="EPSG:3857",
            resolution=250.0,
            bbox=(0.0, 0.0, 10.0, 10.0),
            cloud_fraction=0.5,
            snow_flag=True,
            quality_bitmask=3,
            permanent_water_mask_available=True,
        )
        assert metadata.crs == "EPSG:3857"
        assert metadata.resolution == 250.0
        assert metadata.cloud_fraction == 0.5
        assert metadata.snow_flag is True
        assert metadata.quality_bitmask == 3
        assert metadata.permanent_water_mask_available is True

    def test_cloud_fraction_out_of_range_low(self) -> None:
        """Test that cloud_fraction < 0 is rejected."""
        ts = datetime(2024, 10, 31, tzinfo=timezone.utc)
        with pytest.raises(ValidationError):
            TileMetadata(
                event_id="test",
                source_id="viirs",
                fetch_timestamp=ts,
                bbox=(0, 0, 1, 1),
                cloud_fraction=-0.1,
            )

    def test_cloud_fraction_out_of_range_high(self) -> None:
        """Test that cloud_fraction > 1 is rejected."""
        ts = datetime(2024, 10, 31, tzinfo=timezone.utc)
        with pytest.raises(ValidationError):
            TileMetadata(
                event_id="test",
                source_id="viirs",
                fetch_timestamp=ts,
                bbox=(0, 0, 1, 1),
                cloud_fraction=1.1,
            )

    def test_cloud_fraction_boundary_values(self) -> None:
        """Test that boundary cloud_fraction values are accepted."""
        ts = datetime(2024, 10, 31, tzinfo=timezone.utc)
        m1 = TileMetadata(event_id="test", source_id="viirs", fetch_timestamp=ts, bbox=(0, 0, 1, 1), cloud_fraction=0.0)
        m2 = TileMetadata(event_id="test", source_id="viirs", fetch_timestamp=ts, bbox=(0, 0, 1, 1), cloud_fraction=1.0)
        assert m1.cloud_fraction == 0.0
        assert m2.cloud_fraction == 1.0


class TestSourceMetadata:
    """Tests for SourceMetadata pydantic model."""

    def test_minimal_construction(self) -> None:
        """Test SourceMetadata with required fields only."""
        meta = SourceMetadata(
            source_id="viirs",
            name="VIIRS Flood Product",
            description="VIIRS flood detection at 375m resolution",
        )
        assert meta.source_id == "viirs"
        assert meta.name == "VIIRS Flood Product"
        assert meta.description == "VIIRS flood detection at 375m resolution"
        assert meta.url is None
        assert meta.license is None
        assert meta.temporal_coverage is None
        assert meta.spatial_coverage is None

    def test_full_construction(self) -> None:
        """Test SourceMetadata with all fields."""
        meta = SourceMetadata(
            source_id="gfm",
            name="GFM Flood Product",
            description="Global Flood Monitoring product",
            url="https://example.com/gfm",
            license="CC-BY-4.0",
            temporal_coverage="2016-present",
            spatial_coverage="Global",
        )
        assert meta.url == "https://example.com/gfm"
        assert meta.license == "CC-BY-4.0"
        assert meta.temporal_coverage == "2016-present"
        assert meta.spatial_coverage == "Global"
