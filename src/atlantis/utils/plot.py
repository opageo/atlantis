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
    from matplotlib.patches import Patch

# ── VIIRS pixel code legend ──────────────────────────────────────────────────

VIIRS_CODES: dict[int, tuple[str, str]] = {
    1: ("Fill / No data", "#000000"),
    17: ("Permanent water", "#1f77b4"),
    20: ("Seasonal water", "#17becf"),
    30: ("Cloud", "#cccccc"),
    99: ("Open water", "#4682B4"),
    160: ("Flood (≥60% frac)", "#FF0000"),
}

_DATE_RE = re.compile(r"(\d{4})(\d{2})(\d{2})")


# ── Filename helpers ─────────────────────────────────────────────────────────


def date_from_filename(filename: str) -> str:
    """Extract ``YYYY-MM-DD`` from a VIIRS filename.

    Works with any filename that embeds an 8-digit date token, e.g.
    ``KuroSiwo_1111004_20170828_viirs_flood_extent.tif``.

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
        label = VIIRS_CODES.get(int(unique[i]), ("unknown",))[0]
        code = int(unique[i])
        extra = " (flood)" if 101 <= code <= 200 else ""
        print(f"    {code:3d}  ({label}){extra}: {counts[i]:6,} px  ({pct:.1f}%)")
    flood_px = int(vals_nonzero[(vals_nonzero >= 101) & (vals_nonzero <= 200)].sum())
    print(f"  Flood pixels (101–200): {flood_px:,}")


def pixel_stats_classified(data: np.ndarray, name: str = "flood_extent") -> int:
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


def legend_patches() -> list[Patch]:
    """Return a list of ``matplotlib.patches.Patch`` objects for the VIIRS legend."""
    from matplotlib.patches import Patch

    return [
        Patch(facecolor=color, edgecolor="black", linewidth=0.5, label=f"{code}: {label}")
        for code, (label, color) in VIIRS_CODES.items()
    ]


# ── Plot functions ────────────────────────────────────────────────────────────


def plot_raw(da: "xr.DataArray", title: str, output_path: Path) -> None:  # type: ignore[name-defined]  # noqa: F821
    """Render a raw VIIRS raster with the pixel-code legend as a side panel."""
    import matplotlib.pyplot as plt

    fig, (ax, ax_leg) = plt.subplots(
        1,
        2,
        figsize=(14, 7),
        gridspec_kw={"width_ratios": [3, 1]},
        constrained_layout=True,
    )
    da.plot(ax=ax, cmap="turbo", add_colorbar=True, cbar_kwargs={"label": "Pixel code", "shrink": 0.8})
    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.set_xlabel("Longitude (°)")
    ax.set_ylabel("Latitude (°)")
    ax_leg.legend(
        handles=legend_patches(),
        loc="center",
        fontsize=9,
        title="VIIRS pixel codes",
        title_fontsize=10,
        frameon=True,
        edgecolor="gray",
    )
    ax_leg.axis("off")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {output_path}")


def plot_classified(
    da: "xr.DataArray",  # type: ignore[name-defined]  # noqa: F821
    title: str,
    output_path: Path,
    cmap: str = "Blues",
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
    print(f"  Saved: {output_path}")
