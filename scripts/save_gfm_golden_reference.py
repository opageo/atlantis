"""Phase W.0 of ``.github/prompts/plan-gfmWindowedMemoryFix.prompt.md``.

Generates the golden-reference baseline that every subsequent windowed-
processing change must reproduce (within an empirically-established float
tolerance) before its memory numbers are allowed to matter.

Produces THREE things, all saved under ``scripts/data/`` (gitignored — same
convention as other generated/large data under any ``data/`` directory in
this repo — not committed, but the *script* itself lives in ``scripts/`` so
it stays available across sessions and clones):

1. ``gfm_golden_reference_single.npz`` — full classified-pipeline output for
   the single-item reference cell used throughout this investigation
   (``ENSEMBLE_FLOOD_20241101T060232_VV_EU020M_E036N009T3``).
2. ``gfm_golden_reference_multi.npz`` — same, but for a cell with TWO STAC
   items sharing the same Equi7 tile + solar day
   (``ENSEMBLE_FLOOD_20241101T060232_VV_EU020M_E036N009T3`` +
   ``ENSEMBLE_FLOOD_20241101T060207_VV_EU020M_E036N009T3`` — confirmed via
   direct STAC search, both cover tile EU020M_E036N009T3 on 2024-11-01).
3. Prints the empirical float-reduction-order tolerance baseline (R.4):
   the single-item pipeline run TWICE, nothing changed, diffed against
   itself — the lower bound for "legitimate" noise a windowed comparison
   must beat.

Each .npz also stores the git commit SHA it was generated from (R.5 —
treat any later regeneration as a flagged, explicit decision, not routine).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime

import numpy as np
from loguru import logger

sys.path.insert(0, "src")

from atlantis.fetchers.gfm.backend import DEFAULT_GFM_STAC_URL, GFM_COLLECTION_ID  # noqa: E402
from atlantis.fetchers.gfm.processor import GfmProcessResult, GfmRasterProcessor  # noqa: E402

REFERENCE_BBOX = (-1.5, 38.8, 0.5, 40.0)
REFERENCE_START = "2024-10-29"
REFERENCE_END = "2024-11-04"


def _git_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd="/home/ykalfas/repos/atlantis").decode().strip()


def _find_items(bbox: tuple[float, float, float, float], start: str, end: str) -> list:
    from pystac_client import Client

    catalog = Client.open(DEFAULT_GFM_STAC_URL)
    search = catalog.search(
        collections=GFM_COLLECTION_ID,
        bbox=bbox,
        datetime=(
            datetime.fromisoformat(start),
            datetime.fromisoformat(f"{end}T23:59:59"),
        ),
        max_items=10,
    )
    return list(search.items())


def _result_to_npz_kwargs(result: GfmProcessResult, sha: str) -> dict:
    processed = result.processed
    kwargs: dict = {
        "git_sha": sha,
        "shape": np.array(processed.shape),
        "cloud_fraction": np.array(processed.cloud_fraction),
        "water_fraction": processed.water_fraction,
        "flood_fraction": processed.flood_fraction,
        "reference_water": processed.reference_water,
    }
    for name, arr in processed.extra_layers.items():
        kwargs[f"extra__{name}"] = arr
    return kwargs


def _generate(label: str, items: list, out_path: str, sha: str) -> GfmProcessResult:
    tile_bbox = tuple(items[0].bbox)
    logger.info("[{}] {} item(s), tile bbox {}", label, len(items), tile_bbox)
    processor = GfmRasterProcessor(bbox=tile_bbox, coarsen_factor=4, classify=True)
    result = processor.process_items(
        items,
        event_id="",
        date_token="golden",
        output_dir=None,
        write_outputs=False,
    )
    if result is None:
        raise SystemExit(f"[{label}] process_items() returned None — no valid data")
    np.savez(out_path, **_result_to_npz_kwargs(result, sha))
    logger.info("[{}] saved {} (shape={})", label, out_path, result.processed.shape)
    return result


def main() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    sha = _git_sha()
    logger.info("Git SHA: {}", sha)

    items = _find_items(REFERENCE_BBOX, REFERENCE_START, REFERENCE_END)
    if not items:
        raise SystemExit("No items found for the reference bbox/date range")

    # Group by Equi7 tile + solar day to find the multi-item cell.
    by_key: dict[tuple, list] = {}
    for it in items:
        key = (it.properties.get("Equi7Tile"), it.datetime.date().isoformat() if it.datetime else None)
        by_key.setdefault(key, []).append(it)

    single_key = ("EU020M_E036N009T3", "2024-11-01")
    single_items = [it for it in by_key.get(single_key, []) if "060232" in it.id]
    if not single_items:
        raise SystemExit(f"Reference single item not found among: {[it.id for it in items]}")
    multi_items = by_key.get(single_key, [])
    if len(multi_items) < 2:
        raise SystemExit(f"Expected >=2 items sharing tile+day for the multi-item cell, got: {multi_items}")

    logger.info("Single-item cell: {}", [it.id for it in single_items])
    logger.info("Multi-item cell: {}", [it.id for it in multi_items])

    # 1) Single-item golden reference.
    _generate("single", single_items, "scripts/data/gfm_golden_reference_single.npz", sha)

    # 2) Multi-item golden reference (ascending+descending same-day passes).
    _generate("multi", multi_items, "scripts/data/gfm_golden_reference_multi.npz", sha)

    # 3) Empirical float-tolerance baseline (R.4): re-run the single-item
    # pipeline a second time, nothing changed, and diff against the first run.
    logger.info("Re-running single-item pipeline a second time for the tolerance baseline...")
    tile_bbox = tuple(single_items[0].bbox)
    processor2 = GfmRasterProcessor(bbox=tile_bbox, coarsen_factor=4, classify=True)
    result2 = processor2.process_items(
        single_items,
        event_id="",
        date_token="golden-rerun",
        output_dir=None,
        write_outputs=False,
    )
    if result2 is None:
        raise SystemExit("Second single-item run returned None")

    ref = np.load("scripts/data/gfm_golden_reference_single.npz")
    for field_name in ("water_fraction", "flood_fraction"):
        a = ref[field_name]
        b = getattr(result2.processed, field_name)
        finite = np.isfinite(a) & np.isfinite(b)
        diff = np.abs(a[finite] - b[finite])
        logger.info(
            "Tolerance baseline [{}]: max_abs_diff={:.3e}, mean_abs_diff={:.3e}, n_finite={}",
            field_name,
            float(diff.max()) if diff.size else 0.0,
            float(diff.mean()) if diff.size else 0.0,
            int(diff.size),
        )

    logger.info("Done. Golden references + tolerance baseline saved under scripts/data/.")


if __name__ == "__main__":
    main()
