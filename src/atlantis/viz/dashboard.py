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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from atlantis.archive.reader import ArchiveReader
from atlantis.config import ArchiveConfig

if TYPE_CHECKING:
    import xarray as xr

__all__ = ["build_cube_dashboard", "serve_dashboard", "from_stac", "load_dataset"]

# repo_root/docs/assets/logo-transparent.png (dashboard.py -> viz -> atlantis -> src -> repo_root)
_LOGO_PATH = Path(__file__).resolve().parents[3] / "docs" / "assets" / "logo-transparent.png"


def _has(module: str) -> bool:
    """Return True if *module* is importable without importing it."""
    return importlib.util.find_spec(module) is not None


def _metadata_markdown(
    ds: "xr.Dataset",
    *,
    source: str | None,
    archive_root: str | None,
    stac: str | None,
    archive_config: ArchiveConfig | None,
) -> str:
    """Render a basic-summary Markdown blurb of the Zarr store backing *ds*.

    Includes the store URI, source group, dimension sizes, and a few key group
    attrs (``crs``, ``last_updated``, ``source_id``) when present.
    """
    if stac:
        uri = stac
    else:
        config = archive_config or ArchiveConfig()
        uri = str(archive_root or config.archive_root).rstrip("/") + "/" + config.store

    lines = [
        f"- **Store URI:** `{uri}`",
        f"- **Group:** `{source}`",
    ]
    dims = ", ".join(f"{name}={size}" for name, size in ds.sizes.items())
    lines.append(f"- **Dimensions:** {dims}")

    for key, label in (("crs", "CRS"), ("last_updated", "Last updated"), ("source_id", "Source ID")):
        value = ds.attrs.get(key)
        if value:
            lines.append(f"- **{label}:** `{value}`")

    return "\n".join(lines)


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
    tiles: bool = False,
    cmap: str = "Blues",
    rasterize: bool = True,
    frame_width: int | None = None,
    responsive: bool = True,
    storage_options: dict[str, Any] | None = None,
    archive_config: ArchiveConfig | None = None,
):
    """Build a HoloViz dashboard for the datacube.

    :param str | None source: _description_, defaults to None
    :param xr.Dataset | None ds: _description_, defaults to None
    :param str | None archive_root: _description_, defaults to None
    :param str | None stac: _description_, defaults to None
    :param str var: _description_, defaults to "flood_fraction"
    :param tuple[float, float, float, float] | None bbox: _description_, defaults to None
    :param date | str | None start: _description_, defaults to None
    :param date | str | None end: _description_, defaults to None
    :param bool basemap: _description_, defaults to False
    :param bool tiles: _description_, defaults to False
    :param str cmap: _description_, defaults to "Blues"
    :param bool rasterize: _description_, defaults to True
    :param int frame_width: _description_, defaults to 700
    :param dict[str, Any] | None storage_options: _description_, defaults to None
    :param ArchiveConfig | None archive_config: _description_, defaults to None
    :raises ValueError: _description_
    :raises KeyError: _description_
    :return _type_: _description_
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
        "data_aspect": 1,
        "colorbar": True,
        "title": f"{source or var} — {var}",
    }
    if frame_width is not None:
        opts["frame_width"] = frame_width
    elif responsive:
        opts["responsive"] = True
        opts["min_height"] = 500  # Good default for responsive height

    if "time" in da.dims:
        opts["groupby"] = "time"
    if var == "flood_fraction":
        opts["clim"] = (0.0, 1.0)
    if rasterize:
        if _has("datashader"):
            opts["rasterize"] = True
        else:
            logger.warning("datashader not installed — rendering without server-side rasterisation.")
    if basemap or tiles:
        if _has("geoviews"):
            # geo=True projects the EPSG:4326 grid. Coastlines & borders are drawn
            # as vector overlays *on top* of the (opaque) data so they remain
            # visible — a web-tile basemap alone sits underneath and is hidden.
            opts["geo"] = True
            if basemap:
                opts["coastline"] = "50m"
                opts["features"] = ["borders"]
            if tiles:
                opts["tiles"] = "OSM"
        else:
            logger.warning("geoviews/cartopy not installed — skipping map overlay.")

    return da.hvplot.image(**opts)


def serve_dashboard(
    source: str | None = None,
    *,
    ds: "xr.Dataset | None" = None,
    archive_root: str | None = None,
    stac: str | None = None,
    var: str = "flood_fraction",
    bbox: tuple[float, float, float, float] | None = None,
    start: date | str | None = None,
    end: date | str | None = None,
    storage_options: dict[str, Any] | None = None,
    archive_config: ArchiveConfig | None = None,
    host: str = "localhost",
    port: int = 5006,
    show: bool = True,
    title: str = "Atlantis flood datacube",
    **dashboard_kwargs: Any,
) -> None:
    """Build and serve the dashboard on a local Panel server (blocking).

    Extra keyword arguments (``basemap``, ``tiles``, ``cmap``, ``rasterize``,
    ``frame_width``, ``responsive``) are forwarded to :func:`build_cube_dashboard`.
    """
    import panel as pn

    pn.extension(sizing_mode="stretch_both")  # Configure panel for responsive layouts

    # Load the dataset once so it can be reused for both the plot and the
    # metadata card below (avoids loading the same Zarr store twice).
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

    plot = build_cube_dashboard(source, ds=ds, var=var, **dashboard_kwargs)

    # ── Page header: logo + title, unstretched, sized to the logo ──────────
    header_items: list[Any] = []
    if _LOGO_PATH.exists():
        header_items.append(pn.pane.PNG(str(_LOGO_PATH), height=48, sizing_mode="fixed"))
    header_items.append(pn.pane.Markdown("# Atlantis", sizing_mode="fixed", margin=(0, 10)))
    header = pn.Row(
        *header_items,
        sizing_mode="fixed",
        styles={"align-items": "center", "gap": "12px", "padding": "8px 16px"},
    )

    # `pn.panel(plot)` splits a HoloViews object with a `groupby` widget into
    # a 2-element layout: [0] the map pane, [1] the widget box (time slider).
    layout = pn.panel(plot)
    plot_pane, widget_box = (layout[0], layout[1]) if len(layout) > 1 else (layout, None)
    plot_pane.sizing_mode = "stretch_both"

    # Group the map and a right-hand sub-column (slider + collapsible metadata
    # card) into a single flex row, plot taking 90% and the sub-column 10%.
    if widget_box is not None:
        # Let the widget box size to its own content (no stretch_height) so the
        # slider hugs its "time: ..." label instead of floating in empty space.
        metadata_card = pn.Card(
            pn.pane.Markdown(
                _metadata_markdown(
                    ds, source=source, archive_root=archive_root, stac=stac, archive_config=archive_config
                )
            ),
            title="Zarr store metadata",
            collapsed=True,
            sizing_mode="stretch_width",
        )
        right_col = pn.Column(widget_box, metadata_card, sizing_mode="stretch_width")

        plot_pane.styles = {**getattr(plot_pane, "styles", {}), "flex": "0 0 90%"}
        right_col.styles = {**getattr(right_col, "styles", {}), "flex": "0 0 10%"}
        app_content = pn.Row(
            plot_pane,
            right_col,
            sizing_mode="stretch_both",
            styles={"display": "flex", "flex-direction": "row", "align-items": "flex-start"},
        )
    else:
        app_content = plot_pane

    # Constrain the whole grouped container to the desired viewport fraction.
    app = pn.Column(
        app_content,
        sizing_mode="stretch_both",
        styles={
            "width": "80vw",
            "height": "70vh",
            "margin": "auto",
        },
    )

    page = pn.Column(header, app, sizing_mode="stretch_width")
    pn.serve(page, address=host, port=port, show=show, title=title)


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
