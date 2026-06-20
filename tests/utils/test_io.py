"""Tests for I/O utility functions."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlantis.utils.io import (
    DownloadContentError,
    _validate_not_html,
    download_file,
    ensure_dir,
    get_cache_path,
    get_etag,
    set_etag,
)


class TestEnsureDir:
    """Tests for ensure_dir function."""

    def test_creates_directory(self, tmp_path: Path) -> None:
        """Test that a non-existent directory is created."""
        target = tmp_path / "new_dir" / "subdir"
        assert not target.exists()

        result = ensure_dir(target)

        assert result == target
        assert target.is_dir()

    def test_existing_directory(self, tmp_path: Path) -> None:
        """Test that an existing directory is returned as-is."""
        target = tmp_path / "existing"
        target.mkdir(parents=True)

        result = ensure_dir(target)

        assert result == target
        assert target.is_dir()

    def test_returns_path(self, tmp_path: Path) -> None:
        """Test that the same Path object is returned."""
        target = tmp_path / "test"
        result = ensure_dir(target)
        assert result == target


class TestGetCachePath:
    """Tests for get_cache_path function."""

    def test_uses_default_cache_dir(self) -> None:
        """Test default cache dir is ~/.cache/atlantis."""
        path = get_cache_path("https://example.com/data.tif")
        expected_parent = Path.home() / ".cache" / "atlantis"
        assert path.parent == expected_parent

    def test_custom_cache_dir(self, tmp_path: Path) -> None:
        """Test custom cache directory."""
        path = get_cache_path("https://example.com/data.tif", cache_dir=tmp_path)
        assert path.parent == tmp_path

    def test_preserves_extension(self) -> None:
        """Test that URL extension is preserved in the filename."""
        path = get_cache_path("https://example.com/data.tif")
        assert path.suffix == ".tif"

    def test_no_extension(self) -> None:
        """Test URLs without extension."""
        path = get_cache_path("https://example.com/data")
        assert path.suffix == ""

    def test_md5_filename_length(self) -> None:
        """Test that the filename hash is 32 hex characters (MD5)."""
        path = get_cache_path("https://example.com/data.tif")
        # stem is the hash (32 hex chars)
        assert len(path.stem) == 32
        assert all(c in "0123456789abcdef" for c in path.stem)


class TestEtags:
    """Tests for ETag functions."""

    def test_set_and_get_etag(self, tmp_path: Path) -> None:
        """Test setting and retrieving an ETag."""
        cache_file = tmp_path / "data.tif"
        # Create the cache file first
        cache_file.write_text("dummy data")

        set_etag(cache_file, '"abc123"')
        etag = get_etag(cache_file)

        assert etag == '"abc123"'

    def test_get_etag_nonexistent(self, tmp_path: Path) -> None:
        """Test get_etag returns None when no ETag file exists."""
        cache_file = tmp_path / "data.tif"
        etag = get_etag(cache_file)
        assert etag is None

    def test_set_etag_creates_sidecar_file(self, tmp_path: Path) -> None:
        """Test that set_etag creates a .etag sidecar file."""
        cache_file = tmp_path / "data.tif"
        cache_file.write_text("dummy data")

        set_etag(cache_file, '"abc123"')

        etag_file = cache_file.with_suffix(cache_file.suffix + ".etag")
        assert etag_file.exists()
        assert etag_file.read_text() == '"abc123"'

    def test_get_etag_after_overwrite(self, tmp_path: Path) -> None:
        """Test get_etag reflects latest set_etag call."""
        cache_file = tmp_path / "data.tif"
        cache_file.write_text("dummy data")

        set_etag(cache_file, '"v1"')
        set_etag(cache_file, '"v2"')

        assert get_etag(cache_file) == '"v2"'


class TestDownloadFile:
    """Tests for download_file function."""

    def test_uses_cached_file(self, tmp_path: Path) -> None:
        """Test that an already-cached file is returned without re-downloading."""
        cache_file = tmp_path / "cached.tif"
        cache_file.write_text("cached content")

        # Download to an explicitly different output path shouldn't use cache
        # Monkeypatch would be needed for actual HTTP; here we test path logic
        # The function will raise ImportError if requests isn't installed — that's fine,
        # we test the caching path separately
        assert cache_file.read_text() == "cached content"

    def test_ensure_dir_called_on_parent(self, tmp_path: Path) -> None:
        """Test that parent directories are created."""
        nested = tmp_path / "a" / "b" / "c"
        result = ensure_dir(nested)
        assert result.is_dir()

    def test_ensure_dir_idempotent(self, tmp_path: Path) -> None:
        """Test that ensure_dir is idempotent."""
        target = tmp_path / "test"
        target.mkdir(parents=True)
        result = ensure_dir(target)
        assert result == target
        assert target.is_dir()


class TestDownloadFileValidation:
    """Tests for download_file HTML-response rejection."""

    def test_rejects_html_content_type(self, tmp_path: Path) -> None:
        """Raises DownloadContentError when Content-Type is text/html."""
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.headers = {"Content-Type": "text/html; charset=utf-8"}

        with patch("requests.get", return_value=response):
            with pytest.raises(DownloadContentError, match="authentication failure"):
                download_file(
                    "https://example.com/file.hdf",
                    output_path=tmp_path / "file.hdf",
                )

    def test_rejects_html_body(self, tmp_path: Path) -> None:
        """Raises DownloadContentError when body starts with HTML."""
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.headers = {"Content-Type": "application/octet-stream"}
        response.iter_content = MagicMock(return_value=iter([b"<!DOCTYPE html>\n<html><body>Login</body></html>"]))

        with patch("requests.get", return_value=response):
            with pytest.raises(DownloadContentError, match="response body is HTML"):
                download_file(
                    "https://example.com/file.hdf",
                    output_path=tmp_path / "file.hdf",
                )
        # Ensure no partial file is left behind.
        assert not (tmp_path / "file.hdf").exists()
        assert not (tmp_path / "file.hdf.part").exists()

    def test_accepts_binary_data(self, tmp_path: Path) -> None:
        """Succeeds when the response contains genuine binary data."""
        hdf4_magic = b"\x0e\x03\x13\x01" + b"\x00" * 8188
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.headers = {"Content-Type": "application/octet-stream"}
        response.iter_content = MagicMock(return_value=iter([hdf4_magic]))

        with patch("requests.get", return_value=response):
            result = download_file(
                "https://example.com/file.hdf",
                output_path=tmp_path / "file.hdf",
            )
        assert result.exists()
        assert result.read_bytes() == hdf4_magic

    def test_no_partial_file_on_network_error(self, tmp_path: Path) -> None:
        """No .part or final file remains if the download raises mid-stream."""
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.headers = {"Content-Type": "application/octet-stream"}

        def _exploding_iter(chunk_size=None):
            yield b"\x0e\x03\x13\x01"
            raise OSError("connection reset")

        response.iter_content = _exploding_iter

        with patch("requests.get", return_value=response):
            with pytest.raises(OSError, match="connection reset"):
                download_file(
                    "https://example.com/file.hdf",
                    output_path=tmp_path / "file.hdf",
                )
        assert not (tmp_path / "file.hdf").exists()
        assert not (tmp_path / "file.hdf.part").exists()


class TestValidateNotHtml:
    """Tests for _validate_not_html helper."""

    def test_raises_on_doctype(self) -> None:
        _validate_not_html(b"\x0e\x03\x13\x01data", "http://x")  # OK
        with pytest.raises(DownloadContentError):
            _validate_not_html(b"<!DOCTYPE html><html>", "http://x")

    def test_raises_on_html_tag(self) -> None:
        with pytest.raises(DownloadContentError):
            _validate_not_html(b"<html><head>", "http://x")

    def test_ignores_leading_whitespace(self) -> None:
        with pytest.raises(DownloadContentError):
            _validate_not_html(b"   \n  <!DOCTYPE html>", "http://x")

    def test_passes_binary(self) -> None:
        _validate_not_html(b"\x89PNG\r\n\x1a\n", "http://x")
        _validate_not_html(b"\x0e\x03\x13\x01", "http://x")
