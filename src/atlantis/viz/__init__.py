"""Local HoloViz visualization for the Atlantis Zarr datacube.

Public API:

* :func:`build_cube_dashboard` — build an interactive hvplot image (time slider).
* :func:`serve_dashboard` — serve it on a local Panel server.
* :func:`from_stac` — open the datacube through a STAC catalog's Zarr asset.
* :func:`load_dataset` — read a windowed Dataset directly or via STAC.

Plotting dependencies (hvplot/holoviews/panel/datashader/geoviews) are imported
lazily; install them with ``pip install atlantis[viz]``.
"""

from atlantis.viz.dashboard import (
    build_cube_dashboard,
    from_stac,
    load_dataset,
    serve_dashboard,
)

__all__ = [
    "build_cube_dashboard",
    "serve_dashboard",
    "from_stac",
    "load_dataset",
]
