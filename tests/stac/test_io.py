"""Tests for the fsspec-backed pystac StacIO (object-store catalog I/O)."""

import uuid

import pystac
import pytest

from atlantis.stac import FsspecStacIO
from atlantis.stac.io import FsspecStacIO as FsspecStacIODirect


def test_reexported_from_package():
    """FsspecStacIO is importable from the package root and the module."""
    assert FsspecStacIO is FsspecStacIODirect


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/tmp/catalog.json", False),
        ("relative/catalog.json", False),
        ("file:///tmp/catalog.json", False),
        ("http://example.com/catalog.json", False),
        ("https://example.com/catalog.json", False),
        ("s3://bucket/prefix/catalog.json", True),
        ("gs://bucket/catalog.json", True),
        ("memory:///cube/catalog.json", True),
    ],
)
def test_is_fsspec_href_classification(href, expected):
    """Only non-local, non-HTTP schemes are routed through fsspec."""
    assert FsspecStacIO._is_fsspec_href(href) is expected


def test_local_roundtrip_delegates(tmp_path):
    """Local paths delegate to DefaultStacIO and round-trip unchanged."""
    href = str(tmp_path / "note.json")
    io = FsspecStacIO()
    io.write_text_to_href(href, '{"hello": "world"}')
    assert io.read_text_from_href(href) == '{"hello": "world"}'


def test_memory_roundtrip_via_fsspec():
    """A non-local scheme (memory://, like s3://) round-trips via fsspec."""
    href = f"memory:///atlantis-test/{uuid.uuid4().hex}/note.json"
    io = FsspecStacIO()
    io.write_text_to_href(href, '{"flood": 1}')
    assert io.read_text_from_href(href) == '{"flood": 1}'


def test_pystac_from_file_reads_remote_scheme():
    """pystac.Catalog.from_file reads a non-local href when given FsspecStacIO.

    Reproduces the original failure (``URLSchemeUnknown: s3``) using fsspec's
    in-memory filesystem, which travels the same code path as ``s3://``.
    """
    href = f"memory:///atlantis-test/{uuid.uuid4().hex}/catalog.json"
    io = FsspecStacIO()

    cat = pystac.Catalog(id="atlantis-test", description="remote read")
    cat.set_self_href(href)
    io.save_json(href, cat.to_dict(include_self_link=False))

    loaded = pystac.Catalog.from_file(href, stac_io=io)
    assert loaded.id == "atlantis-test"
