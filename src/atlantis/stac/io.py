"""fsspec-backed :class:`pystac.StacIO` for catalogs on object stores.

pystac's :class:`~pystac.stac_io.DefaultStacIO` only understands local paths and
``http(s)://`` URLs, so reading a catalog from ``s3://`` raises
``URLSchemeUnknown``. Atlantis persists catalogs to object stores, so this StacIO
routes object-store hrefs through :mod:`fsspec` (using the already-required
``s3fs``) while delegating local and HTTP hrefs to the default implementation.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import pystac

__all__ = ["FsspecStacIO"]


class FsspecStacIO(pystac.stac_io.DefaultStacIO):
    """A :class:`pystac.StacIO` that reads/writes object-store hrefs via fsspec.

    Local paths and ``http(s)://`` URLs fall through to
    :class:`pystac.stac_io.DefaultStacIO`; any other scheme (``s3://``, ``gs://``,
    ``az://``, ...) is handled with :func:`fsspec.open` so credentials and custom
    endpoints flow through ``storage_options``.

    Args:
        storage_options: fsspec filesystem options for remote hrefs (credentials,
            ``anon``, ``endpoint_url`` via ``client_kwargs``, ...).
    """

    # Schemes the default StacIO already handles natively.
    _PASSTHROUGH_SCHEMES = frozenset({"", "file", "http", "https"})

    def __init__(self, storage_options: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.storage_options = dict(storage_options or {})

    @classmethod
    def _is_fsspec_href(cls, href: str) -> bool:
        """Return True if *href* needs fsspec (i.e. is an object-store URI)."""
        return urlparse(href).scheme not in cls._PASSTHROUGH_SCHEMES

    def read_text_from_href(self, href: str) -> str:
        if not self._is_fsspec_href(href):
            return super().read_text_from_href(href)
        import fsspec

        with fsspec.open(href, mode="rt", encoding="utf-8", **self.storage_options) as f:
            return f.read()

    def write_text_to_href(self, href: str, txt: str) -> None:
        if not self._is_fsspec_href(href):
            return super().write_text_to_href(href, txt)
        import fsspec

        with fsspec.open(href, mode="wt", encoding="utf-8", **self.storage_options) as f:
            f.write(txt)
