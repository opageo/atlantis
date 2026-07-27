"""Correctness gate for GFM windowed processing.

Runs the windowed classified path (``GfmRasterProcessor(window_size=...)``)
against the same real single-item and multi-item cells used to build the
golden references, at several window sizes, and asserts the output matches
the golden reference within the empirically-established tolerance (measured
as exactly 0.0 for a same-code double-run — see
``scripts/save_gfm_golden_reference.py`` output). ``flood_fraction`` must
match exactly (proven byte-exact after the "assemble, don't buffer" fix);
``water_fraction`` gets one narrow, explicitly-bounded exception for a tiny,
well-understood residual — see ``WATER_FRACTION_EXCEPTION_MAX_PIXELS``/
``WATER_FRACTION_EXCEPTION_MAX_DIFF`` below.

Also asserts the diff is spatially *structureless*: a window-boundary seam
bug's location is mathematically tied to where windows meet, so it MUST move
when ``window_size`` changes. The known residual is a data-dependent artifact
(GDAL resampling sensitivity near a real tile-edge feature) and is
empirically identical across every window size tested. The gate asserts the
exact set of exceeding-pixel locations is IDENTICAL across all window sizes
for a given cell/field -- any window-size-dependent movement is treated as
the signature of a reintroduced seam bug, not the known artifact.

Reusable diagnostic script (kept in ``scripts/``, not ``tmp/``, so it and the
plan documents that reference it stay consistent across sessions/clones —
not part of the permanent test suite; see
``tests/fetchers/gfm/test_processor_memory.py`` for the committed synthetic
regression test).
"""

from __future__ import annotations

import sys
from datetime import datetime

import numpy as np
from loguru import logger

sys.path.insert(0, "src")

from atlantis.fetchers.gfm.backend import DEFAULT_GFM_STAC_URL, GFM_COLLECTION_ID  # noqa: E402
from atlantis.fetchers.gfm.processor import GfmRasterProcessor  # noqa: E402

REFERENCE_BBOX = (-1.5, 38.8, 0.5, 40.0)
REFERENCE_START = "2024-10-29"
REFERENCE_END = "2024-11-04"

# Tolerance: a same-code double-run of the unwindowed path measured an
# *exact* 0.0 diff, so any windowed diff should also be ~0. Allow a tiny
# float32 epsilon for legitimate reduction-order noise from the windowed
# reprojection touching fewer source pixels per call.
TOLERANCE = 1e-5

# A tiny residual remains on water_fraction ONLY: GDAL's `average`-resampling
# is sensitive to the source array's overall extent, perturbing a few pixels
# near the tile's true edge. Bounded carve-out — flood_fraction gets NO
# exception (proven byte-exact).
WATER_FRACTION_EXCEPTION_MAX_PIXELS = 20
WATER_FRACTION_EXCEPTION_MAX_DIFF = 5e-3


def _find_items() -> list:
    from pystac_client import Client

    catalog = Client.open(DEFAULT_GFM_STAC_URL)
    search = catalog.search(
        collections=GFM_COLLECTION_ID,
        bbox=REFERENCE_BBOX,
        datetime=(
            datetime.fromisoformat(REFERENCE_START),
            datetime.fromisoformat(f"{REFERENCE_END}T23:59:59"),
        ),
        max_items=10,
    )
    return list(search.items())


def _check_cell(label: str, items: list, golden_path: str, window_sizes: list[int]) -> bool:
    golden = np.load(golden_path)
    tile_bbox = tuple(items[0].bbox)
    all_ok = True
    # Tracks the exceeding-pixel-location set per field across window sizes,
    # to enforce the window-size-invariance structurelessness check (R.4).
    exceeding_locations: dict[str, set[tuple[int, int]]] = {}

    for window_size in window_sizes:
        processor = GfmRasterProcessor(bbox=tile_bbox, coarsen_factor=4, classify=True, window_size=window_size)
        result = processor.process_items(
            items, event_id="", date_token=f"verify-{window_size}", output_dir=None, write_outputs=False
        )
        if result is None:
            logger.error("[{}] window_size={}: process_items() returned None", label, window_size)
            all_ok = False
            continue

        for field_name in ("water_fraction", "flood_fraction"):
            golden_arr = golden[field_name]
            windowed_arr = getattr(result.processed, field_name)
            if golden_arr.shape != windowed_arr.shape:
                logger.error(
                    "[{}] window_size={} field={}: SHAPE MISMATCH golden={} windowed={}",
                    label,
                    window_size,
                    field_name,
                    golden_arr.shape,
                    windowed_arr.shape,
                )
                all_ok = False
                continue
            finite = np.isfinite(golden_arr) & np.isfinite(windowed_arr)
            diff = np.abs(golden_arr[finite] - windowed_arr[finite])
            max_diff = float(diff.max()) if diff.size else 0.0
            mean_diff = float(diff.mean()) if diff.size else 0.0
            n_exceeding = int((diff > TOLERANCE).sum()) if diff.size else 0

            if n_exceeding == 0:
                passed, status = True, "PASS"
            elif (
                field_name == "water_fraction"
                and n_exceeding <= WATER_FRACTION_EXCEPTION_MAX_PIXELS
                and max_diff <= WATER_FRACTION_EXCEPTION_MAX_DIFF
            ):
                passed, status = True, "PASS (bounded exception)"
            else:
                passed, status = False, "FAIL"
            all_ok = all_ok and passed
            logger.info(
                "[{}] window_size={} field={}: max_abs_diff={:.3e} mean_abs_diff={:.3e} n_finite={} "
                "n_exceeding_tolerance={} -> {}",
                label,
                window_size,
                field_name,
                max_diff,
                mean_diff,
                int(diff.size),
                n_exceeding,
                status,
            )

            # Assert exceeding-pixel locations are identical across window
            # sizes — a seam bug's location moves with window_size; this
            # artifact doesn't.
            full_diff = np.zeros_like(golden_arr, dtype=np.float64)
            mask2d = np.isfinite(golden_arr) & np.isfinite(windowed_arr)
            full_diff[mask2d] = np.abs(golden_arr[mask2d] - windowed_arr[mask2d])
            exceeding_mask = full_diff > TOLERANCE
            nonzero_rows, nonzero_cols = np.nonzero(exceeding_mask)
            this_locations = set(zip(nonzero_rows.tolist(), nonzero_cols.tolist(), strict=True))
            if nonzero_rows.size:
                logger.warning(
                    "[{}] window_size={} field={}: {} pixels exceed tolerance; row range [{}, {}], col range [{}, {}]",
                    label,
                    window_size,
                    field_name,
                    nonzero_rows.size,
                    nonzero_rows.min(),
                    nonzero_rows.max(),
                    nonzero_cols.min(),
                    nonzero_cols.max(),
                )
            prior_locations = exceeding_locations.get(field_name)
            if prior_locations is None:
                exceeding_locations[field_name] = this_locations
            else:
                assert this_locations == prior_locations, (
                    f"[{label}] window_size={window_size} field={field_name}: exceeding-pixel "
                    f"locations changed vs. a prior window_size -- seam bug, not the known artifact"
                )

    return all_ok


def main() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    items = _find_items()
    by_key: dict[tuple, list] = {}
    for it in items:
        key = (it.properties.get("Equi7Tile"), it.datetime.date().isoformat() if it.datetime else None)
        by_key.setdefault(key, []).append(it)

    single_key = ("EU020M_E036N009T3", "2024-11-01")
    single_items = [it for it in by_key.get(single_key, []) if "060232" in it.id]
    multi_items = by_key.get(single_key, [])

    # Three window sizes (5000px/3x3, 3000px/5x5, 1500px/10x10) give a
    # meaningful "does granularity matter" check across a range of window
    # grids over the same 15000x15000 native tile.
    window_sizes = [5000, 3000, 1500]

    ok_single = _check_cell("single-item", single_items, "scripts/data/gfm_golden_reference_single.npz", window_sizes)
    ok_multi = _check_cell("multi-item", multi_items, "scripts/data/gfm_golden_reference_multi.npz", window_sizes)

    if ok_single and ok_multi:
        logger.info("CORRECTNESS GATE: PASS (all window sizes, both cells, within tolerance={:.1e})", TOLERANCE)
        sys.exit(0)
    else:
        logger.error("CORRECTNESS GATE: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    main()
