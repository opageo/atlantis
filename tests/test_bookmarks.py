"""Tests for the static event-bookmark registry (GeoParquet)."""

from datetime import date

import pytest

from atlantis.bookmarks import (
    _resolved_storage_options,
    add_bookmark,
    bookmark_path,
    get_bookmark,
    list_bookmarks,
    load_bookmarks,
    remove_bookmark,
)
from atlantis.models.event import FloodEvent


@pytest.fixture
def bookmarks_path(tmp_path):
    return str(tmp_path / "bookmarks.parquet")


@pytest.fixture
def harvey():
    return FloodEvent(
        event_id="Harvey_2017",
        bbox=(-97.27, 28.24, -95.54, 29.80),
        start_date=date(2017, 8, 28),
        end_date=date(2017, 8, 31),
        sources=["viirs"],
    )


class TestLoadEmpty:
    def test_load_missing_file_returns_empty_frame(self, bookmarks_path):
        gdf = load_bookmarks(path=bookmarks_path)
        assert gdf.empty
        assert set(gdf.columns) == {
            "event_id",
            "start_date",
            "end_date",
            "sources",
            "label",
            "updated_at",
            "geometry",
        }

    def test_list_bookmarks_empty(self, bookmarks_path):
        assert list_bookmarks(path=bookmarks_path) == []

    def test_get_unknown_bookmark_raises(self, bookmarks_path):
        with pytest.raises(KeyError):
            get_bookmark("nope", path=bookmarks_path)


class TestAddGetRoundTrip:
    def test_add_then_get(self, bookmarks_path, harvey):
        dest = add_bookmark(harvey, label="Hurricane Harvey", path=bookmarks_path)
        assert dest == bookmarks_path

        resolved = get_bookmark("Harvey_2017", path=bookmarks_path)
        assert resolved.event_id == "Harvey_2017"
        assert resolved.bbox == pytest.approx(harvey.bbox)
        assert resolved.start_date == harvey.start_date
        assert resolved.end_date == harvey.end_date
        assert resolved.sources == ["viirs"]

    def test_add_persists_to_disk(self, bookmarks_path, harvey):
        add_bookmark(harvey, path=bookmarks_path)
        gdf = load_bookmarks(path=bookmarks_path)
        assert len(gdf) == 1
        assert gdf.crs is not None
        assert gdf.crs.to_epsg() == 4326

    def test_add_duplicate_without_force_raises(self, bookmarks_path, harvey):
        add_bookmark(harvey, path=bookmarks_path)
        with pytest.raises(ValueError, match="already exists"):
            add_bookmark(harvey, path=bookmarks_path)

    def test_add_duplicate_with_overwrite_replaces(self, bookmarks_path, harvey):
        add_bookmark(harvey, path=bookmarks_path)
        updated = FloodEvent(
            event_id="Harvey_2017",
            bbox=(-98.0, 28.0, -95.0, 30.0),
            start_date=date(2017, 8, 27),
            end_date=date(2017, 9, 1),
            sources=["viirs", "modis"],
        )
        add_bookmark(updated, path=bookmarks_path, overwrite=True)

        gdf = load_bookmarks(path=bookmarks_path)
        assert len(gdf) == 1
        resolved = get_bookmark("Harvey_2017", path=bookmarks_path)
        assert resolved.bbox == pytest.approx((-98.0, 28.0, -95.0, 30.0))
        assert resolved.sources == ["viirs", "modis"]

    def test_multiple_bookmarks_list_sorted(self, bookmarks_path, harvey):
        add_bookmark(harvey, path=bookmarks_path)
        add_bookmark(
            FloodEvent(
                event_id="Bihar_2019",
                bbox=(84.84, 24.92, 86.49, 26.16),
                start_date=date(2019, 9, 16),
                end_date=date(2019, 9, 20),
            ),
            path=bookmarks_path,
        )
        assert list_bookmarks(path=bookmarks_path) == ["Bihar_2019", "Harvey_2017"]


class TestRemove:
    def test_remove_unknown_raises(self, bookmarks_path):
        with pytest.raises(KeyError):
            remove_bookmark("nope", path=bookmarks_path)

    def test_remove_existing(self, bookmarks_path, harvey):
        add_bookmark(harvey, path=bookmarks_path)
        remove_bookmark("Harvey_2017", path=bookmarks_path)
        assert list_bookmarks(path=bookmarks_path) == []
        with pytest.raises(KeyError):
            get_bookmark("Harvey_2017", path=bookmarks_path)


class TestDefaultLocation:
    def test_bookmark_path_defaults_to_atlantis_bucket(self):
        assert bookmark_path() == "s3://atlantis/assets/bookmarks.parquet"

    def test_explicit_storage_options_win(self):
        explicit = {"anon": True}
        assert _resolved_storage_options("s3://atlantis/assets/bookmarks.parquet", explicit) is explicit

    def test_atlantis_bucket_gets_ecmwf_endpoint_by_default(self):
        opts = _resolved_storage_options("s3://atlantis/assets/bookmarks.parquet", None)
        assert opts == {"client_kwargs": {"endpoint_url": "https://object-store.os-api.cci1.ecmwf.int"}}

    def test_non_atlantis_remote_path_gets_no_default_options(self):
        assert _resolved_storage_options("s3://some-other-bucket/bookmarks.parquet", None) is None

    def test_local_path_gets_no_default_options(self, bookmarks_path):
        assert _resolved_storage_options(bookmarks_path, None) is None
