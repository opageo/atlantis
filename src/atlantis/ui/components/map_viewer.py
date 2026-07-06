"""Plotly flood map component rendered from a GeoTIFF."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import plotly.express as px
import plotly.graph_objects as go


def flood_map_plotly(
    geotiff_path: Path | None = None,
    data_array: "np.ndarray | None" = None,
    title: str = "Flood Map",
    is_classified: bool = True,
) -> go.Figure | None:
    """Render a flood map as a Plotly figure.

    Args:
        geotiff_path: Path to a harmonised GeoTIFF.
        data_array: Alternatively, a numpy array to render directly.
        title: Chart title.
        is_classified: If True, renders as continuous blue-scale; otherwise
            renders as discrete categorical.

    Returns:
        A Plotly Figure, or None if the input couldn't be read.
    """
    if geotiff_path is not None:
        try:
            import rioxarray as rxr

            da = rxr.open_rasterio(geotiff_path).squeeze(drop=True)
            arr = da.values
        except Exception:
            return None
    elif data_array is not None:
        arr = data_array
    else:
        return None

    if is_classified:
        fig = px.imshow(
            arr,
            color_continuous_scale="Blues",
            title=title,
            zmin=0,
            zmax=max(float(np.nanmax(arr)), 0.01) if np.issubdtype(arr.dtype, np.floating) else int(np.nanmax(arr)),
            aspect="equal",
        )
    else:
        arr_display = np.nan_to_num(arr, nan=0).astype(np.int64)
        fig = px.imshow(
            arr_display,
            title=title,
            aspect="equal",
            color_continuous_scale="Viridis",
        )

    fig.update_layout(
        xaxis_title="Longitude",
        yaxis_title="Latitude",
        margin={"l": 20, "r": 20, "t": 40, "b": 20},
    )
    return fig


def plotly_legend_from_codes(codes: dict[int, tuple[str, str]]) -> list[dict]:
    """Build a list of legend trace dicts for Plotly from pixel-code mappings.

    Args:
        codes: Mapping of ``{pixel_code: (label, hex_color)}``.

    Returns:
        List of dicts suitable for use as legend items in Plotly.
    """
    legend = []
    for code, (label, color) in sorted(codes.items()):
        legend.append({"code": code, "label": label, "color": color})
    return legend
