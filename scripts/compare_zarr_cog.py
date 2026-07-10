"""Compare a deployed VIIRS COG against the consolidated Zarr datacube.

Given a single COG *product* (``s3://.../<DATE>/GLB<NNN>.tif``), this script:

  1. reads the COG (uint8 0-100 percent, 255 = nodata) and takes its geographic
     bounds + date as the area/time of interest;
  2. slices the **same** ``(date, bbox)`` window out of the datacube's ``viirs``
     group via :class:`atlantis.archive.reader.ArchiveReader`;
  3. decodes both to flood-fraction in ``[0, 1]`` and aligns them pixel-for-pixel
     on the canonical 1-arcmin grid;
  4. plots ``COG | Zarr | difference`` with matplotlib and prints parity stats.

Both outputs derive from the same source granule, so they should be identical
(bit-for-bit on the uint8 codes). This is the offline check that the cube and
the COG archive represent the same data.

Run::

    python scripts/compare_zarr_cog.py \
        --cog  s3://atlantis/viirs/jpss_latest/2020/2020-01-01/GLB134.tif \
        --cube s3://atlantis/zarr/viirs_2020_cube

Reads only; writes a PNG locally (``--out``, default ``data/compare_<date>_GLB<NNN>.png``).
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np

#: ECMWF object store endpoint (COGs + cube live here).
_S3_ENDPOINT = "https://object-store.os-api.cci1.ecmwf.int"
#: Storage NODATA for the uint8 flood-fraction encoding.
_NODATA = 255

#: AOI edge bounds ``(west, south, east, north)`` in degrees.
_Bounds = tuple[float, float, float, float]


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--cog",
        default="s3://atlantis/viirs/jpss_latest/2020/2020-01-01/GLB134.tif",
        help="COG product URI (s3:// or local path). Its bounds + date define the AOI.",
    )
    ap.add_argument(
        "--cube",
        default="s3://atlantis/zarr/viirs_2020_cube",
        help="Datacube root (s3:// or local path) holding the 'viirs' group.",
    )
    ap.add_argument("--source", default="viirs", help="Datacube source group to read.")
    ap.add_argument("--endpoint-url", default=_S3_ENDPOINT, help="S3 endpoint for COG + cube.")
    ap.add_argument("--out", default=None, help="Output PNG path (default data/compare_<date>_GLB<NNN>.png).")
    return ap.parse_args()


def _parse_product(uri: str) -> tuple[str, int | None]:
    """Extract ``(date, aoi_id)`` from a ``.../<YYYY-MM-DD>/GLB<NNN>.tif`` key."""
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", uri)
    aoi_match = re.search(r"GLB0*(\d+)\.tif$", uri)
    if not date_match:
        raise ValueError(f"Could not parse a date (YYYY-MM-DD) from: {uri}")
    return date_match.group(1), (int(aoi_match.group(1)) if aoi_match else None)


def _read_cog(uri: str, endpoint_url: str) -> tuple[np.ndarray, _Bounds, np.ndarray, np.ndarray]:
    """Read a COG → ``(flood_fraction[0,1], (w,s,e,n), y_centres, x_centres)``."""
    import rasterio

    if uri.startswith("s3://"):
        import s3fs
        from rasterio.io import MemoryFile

        fs = s3fs.S3FileSystem(endpoint_url=endpoint_url)
        with fs.open(uri, "rb") as f:
            data = f.read()
        with MemoryFile(data) as mem, mem.open() as src:
            u8 = src.read(1)
            bounds, transform = src.bounds, src.transform
    else:
        with rasterio.open(uri) as src:
            u8 = src.read(1)
            bounds, transform = src.bounds, src.transform

    height, width = u8.shape
    x = transform.c + (np.arange(width) + 0.5) * transform.a
    y = transform.f + (np.arange(height) + 0.5) * transform.e
    flood = np.where(u8 == _NODATA, np.nan, u8.astype("float32") / 100.0)
    return flood, (bounds.left, bounds.bottom, bounds.right, bounds.top), y, x


def _read_cube(cube_root: str, source: str, date: str, bbox: _Bounds, endpoint_url: str):
    """Slice the datacube to ``(date, bbox)`` → a decoded flood-fraction DataArray."""
    from atlantis.archive.reader import ArchiveReader

    storage_options = {"endpoint_url": endpoint_url} if cube_root.startswith("s3://") else None
    reader = ArchiveReader(cube_root, storage_options=storage_options)
    ds = reader.read(source, bbox=bbox, start=date, end=date)
    da = ds["flood_fraction"]
    if "time" in da.dims:
        da = da.isel(time=0)
    return da.load()


def _stats(cog: np.ndarray, zarr: np.ndarray) -> dict:
    """Parity statistics over pixels where at least one source is valid."""
    both = ~np.isnan(cog) & ~np.isnan(zarr)
    only_cog = ~np.isnan(cog) & np.isnan(zarr)
    only_zarr = np.isnan(cog) & ~np.isnan(zarr)
    diff = np.where(both, cog - zarr, np.nan)
    n = int(both.sum())
    max_abs = float(np.nanmax(np.abs(diff))) if n else float("nan")
    return {
        "valid_both": n,
        "only_cog": int(only_cog.sum()),
        "only_zarr": int(only_zarr.sum()),
        "max_abs_diff": max_abs,
        "mean_abs_diff": float(np.nanmean(np.abs(diff))) if n else float("nan"),
        "rmse": float(np.sqrt(np.nanmean(diff**2))) if n else float("nan"),
        "exact_uint8": bool(n and max_abs <= 0.005),  # within half a percent → same uint8 code
        "diff": diff,
    }


def main() -> int:
    args = _parse_args()
    date, aoi_id = _parse_product(args.cog)
    aoi_label = f"GLB{aoi_id:03d}" if aoi_id is not None else "AOI"
    print(f"Product   : {args.cog}")
    print(f"  date={date}  aoi={aoi_label}")

    cog, bbox, y, x = _read_cog(args.cog, args.endpoint_url)
    print(f"COG       : shape={cog.shape}  bbox=({bbox[0]:.4f},{bbox[1]:.4f},{bbox[2]:.4f},{bbox[3]:.4f})")
    print(f"  valid pixels={int(np.isfinite(cog).sum())}  range=[{np.nanmin(cog):.3f},{np.nanmax(cog):.3f}]")

    zarr_da = _read_cube(args.cube, args.source, date, bbox, args.endpoint_url)
    zarr = np.asarray(zarr_da.values, dtype="float32")
    print(f"Zarr cube : shape={zarr.shape}  group='{args.source}'  date={date}")
    n_zarr = int(np.isfinite(zarr).sum())
    if n_zarr == 0:
        print(
            "  ⚠ The cube window is entirely empty (no data written for this AOI/date).\n"
            "    Was this granule included in the cube-build partition? Nothing to compare."
        )
        return 1
    print(f"  valid pixels={n_zarr}  range=[{np.nanmin(zarr):.3f},{np.nanmax(zarr):.3f}]")

    if cog.shape != zarr.shape:
        # Align on the canonical grid via xarray coordinates (robust to edge off-by-one).
        import xarray as xr

        cog_da = xr.DataArray(cog, dims=("y", "x"), coords={"y": y, "x": x})
        cog_da, zarr_da = xr.align(cog_da, zarr_da, join="inner")
        cog, zarr = np.asarray(cog_da.values), np.asarray(zarr_da.values)
        y, x = cog_da["y"].values, cog_da["x"].values
        print(f"Aligned   : shape={cog.shape} (inner-join on grid coordinates)")

    s = _stats(cog, zarr)
    print("\nParity (flood-fraction, [0,1]):")
    print(f"  pixels compared      : {s['valid_both']}")
    print(f"  nodata-only-in-COG   : {s['only_cog']}")
    print(f"  nodata-only-in-Zarr  : {s['only_zarr']}")
    print(f"  max |COG - Zarr|     : {s['max_abs_diff']:.4g}")
    print(f"  mean |COG - Zarr|    : {s['mean_abs_diff']:.4g}")
    print(f"  RMSE                 : {s['rmse']:.4g}")
    verdict = "IDENTICAL (same uint8 codes)" if s["exact_uint8"] else "DIFFERENT — inspect the diff panel"
    print(f"  verdict              : {verdict}")

    out = Path(args.out) if args.out else Path("data") / f"compare_{date}_{aoi_label}.png"
    _plot(cog, zarr, s["diff"], bbox, date, aoi_label, s, out)
    print(f"\nFigure → {out}")
    return 0


def _plot(cog, zarr, diff, bbox, date, aoi_label, s, out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    extent = (bbox[0], bbox[2], bbox[1], bbox[3])  # (left, right, bottom, top)
    fig, axes = plt.subplots(1, 3, figsize=(16, 5.5), constrained_layout=True)

    # Shared, data-driven colour range so faint flood signal is visible while
    # keeping the COG and Zarr panels on the same scale for a fair comparison.
    finite = np.concatenate([cog[np.isfinite(cog)], zarr[np.isfinite(zarr)]])
    vmax = float(np.nanpercentile(finite, 99.5)) if finite.size else 1.0
    vmax = max(vmax, 0.05)

    for ax, arr, title in (
        (axes[0], cog, "COG (deployed)"),
        (axes[1], zarr, "Zarr datacube"),
    ):
        im = ax.imshow(arr, extent=extent, origin="upper", cmap="Blues", vmin=0, vmax=vmax)
        ax.set_title(title)
        ax.set_xlabel("lon")
        ax.set_ylabel("lat")
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="flood fraction")

    vmax = max(float(np.nanmax(np.abs(diff))), 1e-6)
    imd = axes[2].imshow(diff, extent=extent, origin="upper", cmap="RdBu_r", vmin=-vmax, vmax=vmax)
    axes[2].set_title(f"COG − Zarr (max|Δ|={s['max_abs_diff']:.3g})")
    axes[2].set_xlabel("lon")
    axes[2].set_ylabel("lat")
    fig.colorbar(imd, ax=axes[2], fraction=0.046, pad=0.04, label="difference")

    verdict = "identical uint8" if s["exact_uint8"] else "DIFFERENT"
    fig.suptitle(f"VIIRS {aoi_label} · {date} · {s['valid_both']} px compared · {verdict}", fontsize=13)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=120)
    plt.close(fig)


if __name__ == "__main__":
    raise SystemExit(main())
