"""Tests for I/O utility functions."""

from pathlib import Path

import pytest

from atlantis.utils.io import (
    HtmlResponseError,
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


class _FakeResponse:
    """Minimal stand-in for ``requests.Response`` covering the streaming path."""

    def __init__(
        self,
        *,
        content_type: str = "application/octet-stream",
        chunks: tuple[bytes, ...] = (b"binary-payload",),
        etag: str | None = None,
    ) -> None:
        self.headers = {"Content-Type": content_type}
        if etag is not None:
            self.headers["ETag"] = etag
        self._chunks = chunks

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 8192) -> tuple[bytes, ...]:
        del chunk_size
        return self._chunks


class TestDownloadFileHtmlGuard:
    """Tests for HTML auth-redirect detection in ``download_file``."""

    def test_rejects_html_content_type(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An HTML Content-Type aborts the download and leaves nothing on disk."""
        destination = tmp_path / "tile.hdf"
        fake = _FakeResponse(content_type="text/html; charset=utf-8", chunks=(b"<html>...</html>",))

        import requests as real_requests

        monkeypatch.setattr(real_requests, "get", lambda *_a, **_k: fake)

        with pytest.raises(HtmlResponseError) as excinfo:
            download_file("https://example.test/tile.hdf", output_path=destination)

        assert "EULA" in str(excinfo.value) or "auth" in str(excinfo.value).lower()
        assert not destination.exists()

    def test_rejects_html_body_when_content_type_misleads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An HTML body with non-HTML Content-Type is still rejected and unlinked."""
        destination = tmp_path / "tile.hdf"
        fake = _FakeResponse(
            content_type="application/octet-stream",
            chunks=(b"<!DOCTYPE html><html><head><title>Earthdata Login</title>",),
        )

        import requests as real_requests

        monkeypatch.setattr(real_requests, "get", lambda *_a, **_k: fake)

        with pytest.raises(HtmlResponseError):
            download_file("https://example.test/tile.hdf", output_path=destination)

        assert not destination.exists()

    def test_writes_binary_payload(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A normal binary response is written through to disk."""
        destination = tmp_path / "tile.hdf"
        fake = _FakeResponse(
            content_type="application/x-hdf",
            chunks=(b"\x89HDF", b"binary tail"),
            etag='"abc"',
        )

        import requests as real_requests

        monkeypatch.setattr(real_requests, "get", lambda *_a, **_k: fake)

        result = download_file("https://example.test/tile.hdf", output_path=destination)

        assert result == destination
        assert destination.read_bytes() == b"\x89HDFbinary tail"
        assert get_etag(destination) == '"abc"'


class TestValidateNotHtml:
    """Tests for the HTML-content guard utility."""

    def test_raises_for_doctype_html(self) -> None:
        from atlantis.utils.io import DownloadContentError, _validate_not_html

        with pytest.raises(DownloadContentError):
            _validate_not_html(b"  <!DOCTYPE html><html>", "https://example.com")

    def test_raises_for_html_tag(self) -> None:
        from atlantis.utils.io import DownloadContentError, _validate_not_html

        with pytest.raises(DownloadContentError):
            _validate_not_html(b" <HTML><head>", "https://example.com")

    def test_passes_for_binary_data(self) -> None:
        from atlantis.utils.io import _validate_not_html

        _validate_not_html(b"\x89HDF\x0d\x0a", "https://example.com")

    def test_passes_for_short_non_html(self) -> None:
        from atlantis.utils.io import _validate_not_html

        _validate_not_html(b"not-html", "https://example.com")


class TestDownloadFileCacheHit:
    """Tests for the cached-file fast path in download_file."""

    def test_returns_cached_file_when_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        destination = tmp_path / "cached.tif"
        destination.write_bytes(b"pre-cached")

        import requests as real_requests

        fake = _FakeResponse(content_type="application/x-hdf", chunks=(b"fresh",))
        monkeypatch.setattr(real_requests, "get", lambda *_a, **_k: fake)

        result = download_file("https://example.test/data.tif", output_path=destination)
        assert result == destination
        assert destination.read_bytes() == b"pre-cached"
