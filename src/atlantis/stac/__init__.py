"""STAC layer for Atlantis.

Two distinct catalogs live here:

* :mod:`atlantis.stac.datacube_catalog` — a static catalog over the consolidated
  Zarr **datacube** (one collection per source, one item per populated date).
* :mod:`atlantis.stac.stac_catalog` — the self-contained **KuroSiwo** SAR catalog
  (GeoTIFF assets on S3); :mod:`atlantis.stac.stac_api` queries external STAC APIs.

Only the datacube builders are re-exported here to keep importing this package
light (the KuroSiwo / API modules pull in boto3 / typer and are imported directly
where needed).
"""

from atlantis.stac.datacube_catalog import (
    BuildProgress,
    build_datacube_catalog,
    build_source_collection,
    write_catalog,
)
from atlantis.stac.io import FsspecStacIO

__all__ = [
    "BuildProgress",
    "FsspecStacIO",
    "build_datacube_catalog",
    "build_source_collection",
    "write_catalog",
]
