"""Tests for Zarr store resolution."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from atlantis.archive._store import is_remote, store_for


class TestIsRemote:
    """Tests for remote URI detection."""

    def test_s3_is_remote(self) -> None:
        assert is_remote("s3://bucket/path") is True

    def test_https_is_remote(self) -> None:
        assert is_remote("https://example.com/data") is True

    def test_local_path_is_not_remote(self) -> None:
        assert is_remote("/data/local/path") is False

    def test_path_object_is_not_remote(self) -> None:
        assert is_remote(Path("/tmp/data")) is False

    def test_file_uri_is_not_remote(self) -> None:
        assert is_remote("file:///data/path") is False

    def test_no_scheme_is_not_remote(self) -> None:
        assert is_remote("data/relative") is False


class TestStoreFor:
    """Tests for store target resolution."""

    def test_local_store_returns_path(self, tmp_path: Path) -> None:
        result = store_for(tmp_path, "raw.zarr")
        assert isinstance(result, Path)
        assert result == tmp_path / "raw.zarr"

    def test_remote_store_uses_fsspec(self) -> None:
        with patch("zarr.storage.FsspecStore.from_url") as mock_from_url:
            mock_from_url.return_value = "fake-store"
            result = store_for("s3://bucket", "cube.zarr")
            mock_from_url.assert_called_once_with("s3://bucket/cube.zarr", storage_options=None)
            assert result == "fake-store"

    def test_remote_store_with_trailing_slash(self) -> None:
        with patch("zarr.storage.FsspecStore.from_url") as mock_from_url:
            mock_from_url.return_value = "fake-store"
            result = store_for("s3://bucket/", "cube.zarr")
            mock_from_url.assert_called_once_with("s3://bucket/cube.zarr", storage_options=None)
            assert result == "fake-store"

    def test_remote_store_with_storage_options(self) -> None:
        with patch("zarr.storage.FsspecStore.from_url") as mock_from_url:
            opts = {"anon": True, "key": "val"}
            store_for("s3://bucket", "cube.zarr", storage_options=opts)
            mock_from_url.assert_called_once_with("s3://bucket/cube.zarr", storage_options=opts)
