"""Tests for the MODIS batch download path (auth/EULA failure detection)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import requests

from atlantis.fetchers.modis import batch_processor as bp
from atlantis.utils.io import DownloadContentError


class _FakeResponse:
    def __init__(self, chunks, *, headers=None, status=200):
        self._chunks = list(chunks)
        self.headers = headers or {"Content-Type": "application/octet-stream"}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def iter_content(self, chunk_size):
        return iter(self._chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, url, **kwargs):
        return self.response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def earthdata_token(monkeypatch):
    monkeypatch.setenv("EARTHDATA_TOKEN", "test-token")


def _patch_session(monkeypatch, response):
    monkeypatch.setattr(bp, "_EarthdataSession", lambda: _FakeSession(response))


def _stray_tempfiles() -> list[Path]:
    return sorted(Path(tempfile.gettempdir()).glob("modis_src_*.hdf"))


def test_probe_accepts_binary_first_chunk(earthdata_token, monkeypatch):
    _patch_session(monkeypatch, _FakeResponse([b"\x89HDF", b"payload"]))
    bp.probe_download("https://example/tile.hdf")


def test_probe_rejects_html_content_type(earthdata_token, monkeypatch):
    _patch_session(
        monkeypatch,
        _FakeResponse([b"<html>login</html>"], headers={"Content-Type": "text/html"}),
    )
    with pytest.raises(DownloadContentError, match="LAADS application token"):
        bp.probe_download("https://example/tile.hdf")


def test_probe_rejects_html_body_sniff(earthdata_token, monkeypatch):
    _patch_session(monkeypatch, _FakeResponse([b"<!DOCTYPE html><html>license</html>"]))
    with pytest.raises(DownloadContentError, match="LAADS application token"):
        bp.probe_download("https://example/tile.hdf")


def test_probe_rejects_empty_body(earthdata_token, monkeypatch):
    _patch_session(monkeypatch, _FakeResponse([]))
    with pytest.raises(DownloadContentError, match="empty body"):
        bp.probe_download("https://example/tile.hdf")


def test_probe_propagates_http_status_rejection(earthdata_token, monkeypatch):
    _patch_session(monkeypatch, _FakeResponse([], status=401))
    with pytest.raises(requests.HTTPError):
        bp.probe_download("https://example/tile.hdf")


def test_download_writes_binary_payload(earthdata_token, monkeypatch):
    _patch_session(monkeypatch, _FakeResponse([b"ab", b"cdef"]))
    path = bp._download_to_tempfile("https://example/tile.hdf", ".hdf")
    try:
        assert path.read_bytes() == b"abcdef"
    finally:
        path.unlink(missing_ok=True)


def test_download_rejects_html_and_cleans_up(earthdata_token, monkeypatch):
    _patch_session(monkeypatch, _FakeResponse([b"<html>login</html>"]))
    before = set(_stray_tempfiles())
    with pytest.raises(DownloadContentError, match="LAADS application token"):
        bp._download_to_tempfile("https://example/tile.hdf", ".hdf")
    assert set(_stray_tempfiles()) == before


def test_download_rejects_html_content_type(earthdata_token, monkeypatch):
    _patch_session(
        monkeypatch,
        _FakeResponse([b"\x89HDF"], headers={"Content-Type": "text/html; charset=utf-8"}),
    )
    with pytest.raises(DownloadContentError, match="HTML page"):
        bp._download_to_tempfile("https://example/tile.hdf", ".hdf")


def test_download_rejects_empty_body(earthdata_token, monkeypatch):
    _patch_session(monkeypatch, _FakeResponse([]))
    with pytest.raises(DownloadContentError, match="empty body"):
        bp._download_to_tempfile("https://example/tile.hdf", ".hdf")
