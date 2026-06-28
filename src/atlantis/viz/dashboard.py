"""Local HoloViz dashboard for the Zarr datacube.

Renders a datacube variable as an interactive map with a **time slider** using
``hvplot`` (HoloViews/Bokeh), optionally rasterised server-side with
``datashader`` and overlaid on a web-tile basemap with ``geoviews``. Served
locally via ``panel``.

Heavy plotting dependencies are imported lazily so importing this module (and the
``atlantis.viz`` package) never requires the ``viz`` extra — only calling the
build/serve functions does.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from typing import TYPE_CHECKING, Any

from loguru import logger

from atlantis.archive.reader import ArchiveReader
from atlantis.config import ArchiveConfig

if TYPE_CHECKING:
    import xarray as xr

__all__ = ["build_cube_dashboard", "serve_dashboard", "from_stac", "load_dataset"]


def _has(module: str) -> bool:
    """Return True if *module* is importable without importing it."""
    return importlib.util.find_spec(module) is not None


def load_dataset(
    source: str,
    *,
    archive_root: str | None = None,
    stac: str | None = None,
    var: str = "flood_fraction",
    bbox: tuple[float, float, float, float] | None = None,
    start: date | str | None = None,
    end: date | str | None = None,
    storage_options: dict[str, Any] | None = None,
    archive_config: ArchiveConfig | None = None,
) -> "xr.Dataset":
    """Load a windowed datacube Dataset, either directly or via a STAC catalog.

    Args:
        source: Source group name (e.g. ``"viirs"``).
        archive_root: Datacube root (local dir or ``s3://``); ignored if *stac* set.
        stac: Optional STAC catalog/collection root to discover the Zarr asset.
        var: Variable of interest (used for STAC discovery / validation).
        bbox: AOI ``(west, south, east, north)``.
        start: Inclusive start date.
        end: Inclusive end date.
        storage_options: fsspec options for remote roots.
        archive_config: Archive configuration override.

    Returns:
        A lazily-loaded, CF-decoded xarray Dataset.
    """
    if stac:
        return from_stac(stac, source, var=var, bbox=bbox, start=start, end=end, storage_options=storage_options)
    archive_config = archive_config or ArchiveConfig()
    root = str(archive_root or archive_config.archive_root)
    reader = ArchiveReader(root, archive_config, storage_options=storage_options)
    return reader.read(source, bbox=bbox, start=start, end=end)


def build_cube_dashboard(
    source: str | None = None,
    *,
    ds: "xr.Dataset | None" = None,
    archive_root: str | None = None,
    stac: str | None = None,
    var: str = "flood_fraction",
    bbox: tuple[float, float, float, float] | None = None,
    start: date | str | None = None,
    end: date | str | None = None,
    basemap: bool = False,
    cmap: str = "Blues",
    rasterize: bool = True,
    frame_width: int = 700,
    storage_options: dict[str, Any] | None = None,
    archive_config: ArchiveConfig | None = None,
):
    """Build an interactive hvplot image of a datacube variable (with time slider).

    Pass an in-memory ``ds`` *or* a ``source`` (read from the archive / STAC).

    Returns:
        A HoloViews object (``DynamicMap``/``Image``) suitable for ``panel.panel``.
    """
    import hvplot.xarray  # noqa: F401 - registers the `.hvplot` accessor

    if ds is None:
        if source is None:
            raise ValueError("Provide either `ds` or `source`.")
        ds = load_dataset(
            source,
            archive_root=archive_root,
            stac=stac,
            var=var,
            bbox=bbox,
            start=start,
            end=end,
            storage_options=storage_options,
            archive_config=archive_config,
        )
    if var not in ds:
        raise KeyError(f"Variable '{var}' not in dataset (available: {list(ds.data_vars)}).")

    da = ds[var]
    opts: dict[str, Any] = {
        "x": "x",
        "y": "y",
        "cmap": cmap,
        "frame_width": frame_width,
        "data_aspect": 1,
        "colorbar": True,
        "title": f"{source or var} — {var}",
    }
    if "time" in da.dims:
        opts["groupby"] = "time"
    if var == "flood_fraction":
        opts["clim"] = (0.0, 1.0)
    if rasterize:
        if _has("datashader"):
            opts["rasterize"] = True
        else:
            logger.warning("datashader not installed — rendering without server-side rasterisation.")
    if basemap:
        if _has("geoviews"):
            opts["geo"] = True
            opts["tiles"] = "OSM"
        else:
            logger.warning("geoviews not installed — skipping basemap overlay.")

    return da.hvplot.image(**opts)


def serve_dashboard(
    source: str | None = None,
    *,
    ds: "xr.Dataset | None" = None,
    host: str = "localhost",
    port: int = 5006,
    show: bool = True,
    title: str = "Atlantis flood datacube",
    **dashboard_kwargs: Any,
) -> None:
    """Build and serve the dashboard on a local Panel server (blocking).

    Extra keyword arguments are forwarded to :func:`build_cube_dashboard`.
    """
    import panel as pn

    plot = build_cube_dashboard(source, ds=ds, **dashboard_kwargs)
    app = pn.panel(plot)
    pn.serve(app, address=host, port=port, show=show, title=title)


def from_stac(
    catalog: str,
    source: str | None = None,
    *,
    var: str = "flood_fraction",
    bbox: tuple[float, float, float, float] | None = None,
    start: date | str | None = None,
    end: date | str | None = None,
    storage_options: dict[str, Any] | None = None,
) -> "xr.Dataset":
    """Open the datacube via a STAC catalog's Zarr asset (using xpystac).

    Resolves the collection for *source*, opens its ``zarr`` asset as xarray, and
    applies the same bbox/time windowing as the direct reader.
    """
    import pystac

    try:
        import xpystac  # noqa: F401 - registers the "stac" xarray backend engine
    except ImportError as exc:  # pragma: no cover - optional dep
        raise ImportError("from_stac requires the 'stac' extra (xpystac). Install atlantis[stac].") from exc

    import xarray as xr

    from atlantis.stac.io import FsspecStacIO

    href = catalog if str(catalog).rstrip("/").endswith(".json") else str(catalog).rstrip("/") + "/catalog.json"
    # DefaultStacIO can't read s3:// (URLSchemeUnknown); route remote hrefs via fsspec.
    cat = pystac.Catalog.from_file(href, stac_io=FsspecStacIO(storage_options))

    collection = None
    for child in cat.get_children():
        if source is None or child.id == source or child.id.endswith(f"-{source}"):
            collection = child
            break
    if collection is None:
        raise KeyError(f"No collection for source '{source}' in {href}.")

    asset = collection.assets.get("zarr")
    if asset is None:
        first_item = next(iter(collection.get_items()), None)
        asset = first_item.assets.get("zarr") if first_item is not None else None
    if asset is None:
        raise KeyError(f"No 'zarr' asset on collection '{collection.id}' or its items.")

    ds = xr.open_dataset(asset, engine="stac")

    if bbox is not None:
        from atlantis.archive import grid

        win = grid.bounds_to_window(*bbox)
        ds = ds.isel(y=slice(win.row_start, win.row_stop), x=slice(win.col_start, win.col_stop))
    if start is not None or end is not None:
        import numpy as np

        s = np.datetime64(start, "ns") if start is not None else None
        e = np.datetime64(end, "ns") if end is not None else None
        ds = ds.sel(time=slice(s, e))
    return ds
