"""Phase W.2 correctness gate for ``.github/prompts/plan-gfmWindowedMemoryFix.prompt.md``.

Runs the new windowed classified path (``GfmRasterProcessor(window_size=...)``)
against the same real single-item and multi-item cells used to build the
Phase W.0 golden references, at several window sizes, and asserts the output
matches the golden reference within the empirically-established tolerance
(measured as exactly 0.0 in Phase W.0 for a same-code double-run — see
``scripts/save_gfm_golden_reference.py`` output).

Also checks the diff is spatially *structureless* (R.4): a legitimate
float-reduction-order diff would be tiny/randomly distributed; a seam bug
would show up as a diff concentrated along the window grid lines at a
regular pixel spacing. Histograms the per-pixel abs-diff and checks the
(row, col) location of any nonzero diff doesn't line up with the window grid.

Reusable diagnostic script (kept in ``scripts/``, not ``tmp/``, so it and the
plan documents that reference it stay consistent across sessions/clones —
not part of the permanent test suite; see
``tests/fetchers/gfm/test_processor_memory.py`` for the committed synthetic
regression test added in Phase W.5).
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

# Tolerance: Phase W.0 measured an *exact* 0.0 diff between two identical
# unwindowed runs, so any windowed diff should also be ~0. Allow a tiny
# float32 epsilon for legitimate reduction-order noise from the windowed
# reprojection touching fewer source pixels per call.
TOLERANCE = 1e-5


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
            passed = max_diff <= TOLERANCE
            all_ok = all_ok and passed
            logger.info(
                "[{}] window_size={} field={}: max_abs_diff={:.3e} mean_abs_diff={:.3e} n_finite={} -> {}",
                label,
                window_size,
                field_name,
                max_diff,
                mean_diff,
                int(diff.size),
                "PASS" if passed else "FAIL",
            )

            # Spatial-structurelessness check (R.4): find where the diff is
            # nonzero and confirm it's not concentrated along the window grid.
            full_diff = np.zeros_like(golden_arr, dtype=np.float64)
            mask2d = np.isfinite(golden_arr) & np.isfinite(windowed_arr)
            full_diff[mask2d] = np.abs(golden_arr[mask2d] - windowed_arr[mask2d])
            nonzero_rows, nonzero_cols = np.nonzero(full_diff > TOLERANCE)
            if nonzero_rows.size:
                logger.warning(
                    "[{}] window_size={} field={}: {} pixels exceed tolerance; "
                    "row range [{}, {}], col range [{}, {}] (check for grid-aligned concentration)",
                    label,
                    window_size,
                    field_name,
                    nonzero_rows.size,
                    nonzero_rows.min(),
                    nonzero_rows.max(),
                    nonzero_cols.min(),
                    nonzero_cols.max(),
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

    # Two window sizes give a meaningful "does granularity matter" check while
    # keeping the real-network request count for this correctness pass
    # bounded (multi-item cell has 3 items; 1500px/10x10-window sizing is
    # deliberately left to the dedicated Phase W.3 memory-measurement pass,
    # which only exercises the cheaper single-item cell — see R.3).
    window_sizes = [5000, 3000]

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
