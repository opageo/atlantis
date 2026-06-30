"""Visualisation utilities for VIIRS flood data.

Provides matplotlib helpers for plotting raw and classified VIIRS rasters,
pixel-code statistics, and the standard VIIRS legend.  These are used by
the demo script and optionally by CLI commands with ``--plot``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

# Plotting helpers only ever save figures via fig.savefig; we never display.
# Pin matplotlib to the headless Agg backend before pyplot is imported so the
# CLI doesn't pick up an interactive (Tk) backend, which crashes at shutdown
# with "Tcl_AsyncDelete: async handler deleted by the wrong thread" when the
# rich.console.status spinner thread tears down concurrently.
os.environ.setdefault("MPLBACKEND", "Agg")

import numpy as np

if TYPE_CHECKING:
    import xarray as xr
    from matplotlib.patches import Patch

# ── VIIRS pixel code legend ──────────────────────────────────────────────────

VIIRS_RAW_CODES: dict[int, tuple[str, str]] = {
    1: ("Fill / No data", "#000000"),
    17: ("Vegetation", "#2ca02c"),
    20: ("Snow / ice", "#17becf"),
    30: ("Cloud", "#cccccc"),
    99: ("Permanent water", "#1f77b4"),
    160: ("Flood (codes 101–200, ≥60% frac)", "#FF0000"),
}

# GFM native ensemble_flood_extent band codes
GFM_ENSEMBLE_FLOOD_EXTENT_CODES: dict[int, tuple[str, str]] = {
    0: ("Dry", "#d4c5a9"),
    1: ("Flood", "#FF4444"),
    255: ("No data", "#000000"),
}

# GFM native reference_water_mask band codes
GFM_REFERENCE_WATER_MASK_CODES: dict[int, tuple[str, str]] = {
    0: ("Land", "#d4c5a9"),
    1: ("Water", "#1f77b4"),
    2: ("Permanent water", "#08306b"),
    255: ("No data", "#000000"),
}

# MODIS MCDWD raw band pixel codes
MODIS_RAW_CODES: dict[int, tuple[str, str]] = {
    0: ("No water", "#d4c5a9"),
    1: ("Surface water", "#1f77b4"),
    2: ("Recurring flood", "#FFA500"),
    3: ("Unusual flood", "#FF0000"),
    255: ("Insufficient data", "#000000"),
}

_DATE_RE = re.compile(r"(\d{4})(\d{2})(\d{2})")


# ── Filename helpers ─────────────────────────────────────────────────────────


def date_from_filename(filename: str) -> str:
    """Extract ``YYYY-MM-DD`` from a VIIRS filename.

    Works with any filename that embeds an 8-digit date token, e.g.
    ``KuroSiwo_1111004_20170828_viirs_flood_fraction.tif``.

    Returns ``"unknown"`` if no 8-digit sequence is found.
    """
    m = _DATE_RE.search(filename)
    if m is None:
        return "unknown"
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


# ── Pixel statistics ─────────────────────────────────────────────────────────


def pixel_stats_raw(data: np.ndarray, name: str = "raw") -> None:
    """Print a frequency breakdown of raw VIIRS pixel codes."""
    vals = data.ravel()
    vals_nonzero = vals[vals > 0]
    if len(vals_nonzero) == 0:
        print(f"  {name}: all pixels are nodata (0).")
        return
    unique, counts = np.unique(vals_nonzero, return_counts=True)
    order = np.argsort(-counts)
    print(f"  {name}: non-zero {len(vals_nonzero):,} / {len(vals):,} ({100 * len(vals_nonzero) / len(vals):.1f}%)")
    print("  Top pixel codes:")
    for i in order[:8]:
        pct = 100 * counts[i] / len(vals_nonzero)
        label = VIIRS_RAW_CODES.get(int(unique[i]), ("unknown",))[0]
        code = int(unique[i])
        extra = " (flood)" if 101 <= code <= 200 else ""
        print(f"    {code:3d}  ({label}){extra}: {counts[i]:6,} px  ({pct:.1f}%)")
    flood_px = int(vals_nonzero[(vals_nonzero >= 101) & (vals_nonzero <= 200)].sum())
    print(f"  Flood pixels (101–200): {flood_px:,}")


def pixel_stats_classified(data: np.ndarray, name: str = "flood_fraction") -> int:
    """Print stats for a classified flood array; return the number of flooded pixels."""
    vals = data.ravel()
    valid = vals[~np.isnan(vals)]
    if len(valid) == 0:
        print(f"  {name}: all NaN")
        return 0
    flooded = int((valid > 0).sum())
    print(
        f"  {name}: min={float(valid.min()):.3f}, max={float(valid.max()):.3f}, "
        f"mean={float(valid.mean()):.4f}, "
        f"flooded={flooded:,}/{len(valid):,} px"
    )
    return flooded


# ── Legend ───────────────────────────────────────────────────────────────────


def legend_patches(codes: "dict[int, tuple[str, str]] | None" = None) -> list[Patch]:
    """Return a list of ``matplotlib.patches.Patch`` objects for the given pixel-code legend.

    Args:
        codes: Mapping of ``{pixel_code: (label, hex_colour)}``.  Defaults to
            :data:`VIIRS_RAW_CODES` when *None*.
    """
    from matplotlib.patches import Patch

    if codes is None:
        codes = VIIRS_RAW_CODES
    return [
        Patch(facecolor=color, edgecolor="black", linewidth=0.5, label=f"{code}: {label}")
        for code, (label, color) in codes.items()
    ]


# ── Plot functions ────────────────────────────────────────────────────────────


def plot_raw(
    da: "xr.DataArray",
    title: str,
    output_path: Path,
    *,
    codes: "dict[int, tuple[str, str]] | None" = None,
    legend_title: str = "VIIRS pixel codes",
    announce: bool = True,
) -> None:  # type: ignore[name-defined]  # noqa: F821
    """Render a raw raster with a pixel-code legend as a side panel.

    Args:
        da: The DataArray to plot.
        title: Figure title.
        output_path: Where to save the PNG.
        codes: Pixel-code legend mapping.  Defaults to :data:`VIIRS_RAW_CODES`.
        legend_title: Title for the legend panel.
        announce: Whether to print the output path.
    """
    import matplotlib.pyplot as plt

    legend_codes = codes if codes is not None else VIIRS_RAW_CODES
    fig, (ax, ax_leg) = plt.subplots(
        1,
        2,
        figsize=(14, 7),
        gridspec_kw={"width_ratios": [3, 1]},
        constrained_layout=True,
    )
    if codes is not None:
        # Discrete categorical render: map each pixel code to its exact legend
        # colour so what's drawn matches the legend (avoids a continuous colormap
        # spreading sparse codes like 0/1/255 across the full gradient).
        from matplotlib.colors import BoundaryNorm, ListedColormap

        sorted_codes = sorted(legend_codes)
        colours = [legend_codes[c][1] for c in sorted_codes]
        bounds = [sorted_codes[0] - 0.5]
        bounds += [(sorted_codes[i] + sorted_codes[i + 1]) / 2 for i in range(len(sorted_codes) - 1)]
        bounds += [sorted_codes[-1] + 0.5]
        cmap = ListedColormap(colours)
        norm = BoundaryNorm(bounds, cmap.N)
        da.plot(ax=ax, cmap=cmap, norm=norm, add_colorbar=False)
    else:
        da.plot(ax=ax, cmap="turbo", add_colorbar=True, cbar_kwargs={"label": "Pixel code", "shrink": 0.8})
    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.set_xlabel("Longitude (°)")
    ax.set_ylabel("Latitude (°)")
    ax_leg.legend(
        handles=legend_patches(codes),
        loc="center",
        fontsize=9,
        title=legend_title,
        title_fontsize=10,
        frameon=True,
        edgecolor="gray",
    )
    ax_leg.axis("off")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    if announce:
        print(f"  Saved: {output_path}")


def plot_classified(
    da: "xr.DataArray",  # type: ignore[name-defined]  # noqa: F821
    title: str,
    output_path: Path,
    cmap: str = "Blues",
    *,
    announce: bool = True,
) -> None:
    """Render a classified flood raster (binary 0/1 or flood fraction 0–1)."""
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 7), constrained_layout=True)
    vmax = float(da.max())
    da.plot(
        ax=ax,
        cmap=cmap,
        vmin=0.0,
        vmax=max(vmax, 0.01),
        add_colorbar=True,
        cbar_kwargs={"label": "Flood", "shrink": 0.8},
    )
    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.set_xlabel("Longitude (°)")
    ax.set_ylabel("Latitude (°)")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    if announce:
        print(f"  Saved: {output_path}")
