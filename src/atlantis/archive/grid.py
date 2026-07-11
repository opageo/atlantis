"""Canonical 1-arcmin global grid (EPSG:4326) and AOI → index mapping.

All harmonised flood rasters are snapped to this single global grid (the same
grid used by ECMWF ``Globe_flood_area_*.grb`` / ``CMF_all.zarr``). Defining it
once here lets the archive place any per-event AOI window into a shared global
datacube via integer-index region writes — the foundation of the consolidated
Zarr store.

Grid definition (pixel-**centre** convention):

* resolution: ``1/60`` degrees (1 arc-minute)
* origin: western edge ``-180``, northern edge ``+90``
* shape: ``10800`` rows (lat, north→south) × ``21600`` cols (lon, west→east)
* pixel ``(j, i)`` centre: ``lon = -180 + (i + 0.5)/60``, ``lat = +90 - (j + 0.5)/60``

The snapping math mirrors
:meth:`atlantis.harmoniser.reprojector.Reprojector._snap_bounds_to_global_grid`
so reader-side bbox windows reproduce exactly what the harmoniser wrote.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Grid spacing in degrees (1 arc-minute).
GLOBAL_RESOLUTION: float = 1.0 / 60.0
#: Western edge of column 0.
ORIGIN_LON: float = -180.0
#: Northern edge of row 0 (latitude counts downward from here).
ORIGIN_LAT: float = 90.0
#: Number of columns (longitude) across the full globe.
GLOBAL_WIDTH: int = 21600
#: Number of rows (latitude) across the full globe.
GLOBAL_HEIGHT: int = 10800
#: Coordinate reference system of the canonical grid.
GLOBAL_CRS: str = "EPSG:4326"

#: Absolute tolerance (degrees) for coordinate-alignment checks.
_ALIGN_TOL: float = 1e-6


def global_x_coords() -> np.ndarray:
    """Return the longitude pixel-centre coordinates (size ``GLOBAL_WIDTH``)."""
    return ORIGIN_LON + (np.arange(GLOBAL_WIDTH) + 0.5) * GLOBAL_RESOLUTION


def global_y_coords() -> np.ndarray:
    """Return the latitude pixel-centre coordinates, north→south (size ``GLOBAL_HEIGHT``)."""
    return ORIGIN_LAT - (np.arange(GLOBAL_HEIGHT) + 0.5) * GLOBAL_RESOLUTION


def snap_bounds(
    west: float,
    south: float,
    east: float,
    north: float,
) -> tuple[float, float, float, float]:
    """Snap AOI **edge** bounds outward to the canonical global grid.

    Mirrors the harmoniser's snapping so a bbox maps to the same pixel edges
    the harmonised raster was written on.

    Args:
        west: Western edge of the AOI (degrees).
        south: Southern edge of the AOI (degrees).
        east: Eastern edge of the AOI (degrees).
        north: Northern edge of the AOI (degrees).

    Returns:
        Snapped ``(west, south, east, north)`` clipped to the global extent.
    """
    res = GLOBAL_RESOLUTION
    west_s = ORIGIN_LON + np.floor((west - ORIGIN_LON) / res) * res
    east_s = ORIGIN_LON + np.ceil((east - ORIGIN_LON) / res) * res
    north_s = ORIGIN_LAT - np.floor((ORIGIN_LAT - north) / res) * res
    south_s = ORIGIN_LAT - np.ceil((ORIGIN_LAT - south) / res) * res

    west_s = max(west_s, ORIGIN_LON)
    east_s = min(east_s, ORIGIN_LON + 360.0)
    south_s = max(south_s, ORIGIN_LAT - 180.0)
    north_s = min(north_s, ORIGIN_LAT)
    return float(west_s), float(south_s), float(east_s), float(north_s)


@dataclass(frozen=True)
class IndexWindow:
    """Half-open integer pixel window into the global grid.

    Rows run north→south (``y``), columns west→east (``x``).
    """

    row_start: int
    row_stop: int
    col_start: int
    col_stop: int

    @property
    def height(self) -> int:
        """Number of rows (latitude pixels) in the window."""
        return self.row_stop - self.row_start

    @property
    def width(self) -> int:
        """Number of columns (longitude pixels) in the window."""
        return self.col_stop - self.col_start

    def __post_init__(self) -> None:
        """Validate that the window lies within the global grid extent."""
        if not (0 <= self.row_start < self.row_stop <= GLOBAL_HEIGHT):
            raise ValueError(
                f"Row window [{self.row_start}, {self.row_stop}) outside global grid [0, {GLOBAL_HEIGHT}]."
            )
        if not (0 <= self.col_start < self.col_stop <= GLOBAL_WIDTH):
            raise ValueError(f"Col window [{self.col_start}, {self.col_stop}) outside global grid [0, {GLOBAL_WIDTH}].")


def bounds_to_window(west: float, south: float, east: float, north: float) -> IndexWindow:
    """Map AOI edge bounds to an :class:`IndexWindow` on the global grid."""
    w, s, e, n = snap_bounds(west, south, east, north)
    col_start = int(round((w - ORIGIN_LON) / GLOBAL_RESOLUTION))
    col_stop = int(round((e - ORIGIN_LON) / GLOBAL_RESOLUTION))
    row_start = int(round((ORIGIN_LAT - n) / GLOBAL_RESOLUTION))
    row_stop = int(round((ORIGIN_LAT - s) / GLOBAL_RESOLUTION))
    return IndexWindow(row_start, row_stop, col_start, col_stop)


def coords_to_window(y: np.ndarray, x: np.ndarray) -> IndexWindow:
    """Map a harmonised raster's pixel-centre coordinates to an :class:`IndexWindow`.

    The coordinates must already be snapped to the canonical grid (they are, when
    produced by the harmoniser with ``snap_to_global_grid=True``).

    Args:
        y: Latitude pixel-centre coordinates (north→south).
        x: Longitude pixel-centre coordinates (west→east).

    Returns:
        The :class:`IndexWindow` covering these coordinates.

    Raises:
        ValueError: If the coordinates are not aligned to the global grid.
    """
    y = np.asarray(y, dtype="float64")
    x = np.asarray(x, dtype="float64")
    col_start = int(round((float(x[0]) - ORIGIN_LON) / GLOBAL_RESOLUTION - 0.5))
    row_start = int(round((ORIGIN_LAT - float(y[0])) / GLOBAL_RESOLUTION - 0.5))
    window = IndexWindow(row_start, row_start + y.size, col_start, col_start + x.size)

    expected_x = global_x_coords()[window.col_start : window.col_stop]
    expected_y = global_y_coords()[window.row_start : window.row_stop]
    if not (np.allclose(expected_x, x, atol=_ALIGN_TOL) and np.allclose(expected_y, y, atol=_ALIGN_TOL)):
        raise ValueError(
            "Dataset coordinates are not aligned to the canonical 1-arcmin global grid; "
            "harmonise with snap_to_global_grid=True before archiving."
        )
    return window
