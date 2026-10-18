# Dead-Code Detection & Cleanup Plan

Status: partially done (2026-10, pre-release, `ref/cleanup`)
Branch: `chore/dead-code-cleanup` (plan), applied on `ref/cleanup`

## Progress (pre-release pass — safe removals only)

- **Done — Batch A:** `vulture` is a dev dependency, configured in
  `[tool.vulture]`, runnable as `pixi run deadcode` / `poe deadcode`. Every
  current finding is recorded in `vulture_whitelist.py`, so a clean run means
  no _new_ dead code.
- **Done — zero-reference removals:** `cli._plot_gfm`, `GFM_STAC_CFG`,
  `LANCE_NRT_HDF_PRODUCT`, `viirs.backend.RemoteFile`,
  `viirs.dataset.processed_tiles_to_multi_dataset` (also broken: it read
  `.processed` off a `ProcessedTile`). `GEOIDFLOOD_HF_REPO` is now used to build
  `GEOIDFLOOD_HF_BASE_URL` instead of being removed.
- **Done elsewhere on `ref/cleanup`:** placeholder validators raise
  `NotImplementedError` instead of reporting a fake pass; `is_lfs_pointer`
  deduplicated into `utils/io.py`; peak-window selection shared via
  `fetchers/_selection.py`; unused `ml` extra removed.
- **Deferred until after the first release:** everything below that touches
  public API, test-only helpers, documented constants or "planned" modules
  (Batches B–E items not listed above). They are in the whitelist's
  "Baseline" section — remove an entry there when the code is deleted.
  Scope: adopt a dead-code scanner and remove dead / test-only code from `src/atlantis`.

## Goal

Introduce **Vulture** as the dead-code detector, add a curated whitelist for known
framework false positives, and delete the dead code it surfaces in reviewable batches
without breaking production behaviour.

## Tool Evaluation (already trialed on this repo)

| Tool        | Result on this repo                                                                                                                                                   |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Vulture** | 62 real candidates (111 raw incl. Typer CLI noise). Pure Python, pyproject `[tool.vulture]` support, `# noqa: V1xx` per-line suppression, whitelist file. **Chosen.** |
| `deadcode`  | Coarser detection; `--fix` only handles imports/variables, no functions/methods/classes.                                                                              |
| `skylos`    | Framework-aware and broad, but 1.7 MB wheel + heavy transitive deps; overkill for this task.                                                                          |

Command used for the audit (run from repo root against a throwaway venv):

```bash
python -m vulture src/atlantis scripts tests \
  --min-confidence 60 \
  --ignore-decorators "@*.command,@*.callback,@*.page,@register_fetcher"
```

Audit classification (from a full-repo `rg` sweep):

- **~20 truly dead** symbols (zero references anywhere).
- **~16 test-only groups** (referenced only by tests, or production callers never
  read the written value).
- Remaining flags are **false positives**: framework hooks (Dask `WorkerPlugin.setup`,
  NiceGUI `@ui.page` handlers), third-party widget attributes (`btn.enabled`,
  `panel.sizing_mode`), Pydantic fields set as constructor kwargs, and an intentional
  GC-pin attribute (`src._atlantis_memfile`).

## Batch A — Wire up tooling

1. Create throwaway venv outside the repo for audit runs (Termux-friendly, pure Python).
2. Add dev dependency `vulture>=2.16` to:
   - `pyproject.toml` `[dependency-groups].dev`
   - `pixi.toml` `[feature.dev.dependencies]`
3. Add `[tool.vulture]` to `pyproject.toml`:

   ```toml
   [tool.vulture]
   paths = ["src/atlantis", "vulture_whitelist.py"]
   min_confidence = 60
   ignore_decorators = ["@*.command", "@*.callback", "@*.page", "@register_fetcher"]
   ```

4. Add a `deadcode` task to `pixi.toml` (`[tasks]`) and `[tool.poe.tasks]`;
   optionally a `Makefile` target.
5. Create root `vulture_whitelist.py` for validated false positives:
   - `_.setup` — Dask `WorkerPlugin` callback (`batch/orchestrator.py:37`)
   - `_index` — NiceGUI `@ui.page("/")` handler (`ui/app.py:37`)
   - `_.enabled`, `_.sizing_mode` — NiceGUI / Panel widget properties
   - `_.env_settings` — Pydantic `settings_customise_sources` signature
   - `_._atlantis_memfile` — intentional MemoryFile GC pin (`modis/processor.py:326`)
   - `ItemCollection` — TYPE_CHECKING string annotation (`gfm/backend.py:14`)
   - `TileMetadata` write-only-in-prod fields: `fetch_timestamp`, `quality_bitmask`,
     `permanent_water_mask_available`

## Batch B — Dead code in fetchers

- `gfm/processor.py`:
  - `GFM_STAC_CFG` constant (never passed to `odc.stac.load`).
  - Write-only `GfmCoverageDiagnostics.item_count` / `item_ids` (dataclass fields +
    both construction sites).
  - Unused `reference_native` parameter of `_build_native_masks` (update the production
    call and the three tests).
- `modis/backend.py`: `LANCE_NRT_HDF_PRODUCT`; write-only `_LaadsShortname.layout_dir`
  (+ constructor args).
- `modis/processor.py`: `MODIS_PIXEL_SIZE_DEG`.
- `modis/selection.py`: `cloud_aware_score` (+ tests, `docs/modis/pipeline.md`).
- `viirs/backend.py`: `RemoteFile`.
- `viirs/dataset.py`: `processed_tiles_to_multi_dataset`.
- `viirs/layers.py`: `FLOOD_MIN_CODE`, `SEASONAL_WATER_CODES`, `OPEN_WATER_CODES`
  (+ re-exports in `viirs/processor.py`, notebook import in
  `notebooks/drafts/validate_viirs_legend.ipynb`).
- `viirs/processor.py`: `classify_viirs_flood_fraction` (+ `docs/archive/cog-build.md`).
- `harmoniser/reprojector.py`: `validate_crs`.

## Batch C — Dead code in archive / utils / ui

- `archive/grid.py`: `aoi_blocks`, `aoi_block_id` (+ `tests/archive/test_grid.py`).
  Note: docstring claims they are "retained as generic grid utilities"; there are zero
  production callers.
- `archive/writer.py`: `write_checkpoint`, `is_checkpointed` (+ tests, `config.py`
  `checkpoint_dir`).
- `archive/update.py`: `_modis_group` back-compat alias (+ tests).
- `utils/checklist.py`: `warn_substep`, loguru sink `flush` stub, write-only `_console`.
- `utils/geoidflood.py`: `GEOIDFLOOD_HF_REPO`.
- `utils/geo.py`: `bbox_area`; `utils/io.py`: `_validate_not_html`, `get_etag`;
  `utils/ui.py`: `skip` (+ their tests).
- `stac/stac_api.py`: `KS_CRS` only (keep the module — it is a KuroSiwo design artifact).
- `ui/pages/history.py`: delete the whole orphaned module (never routed from `ui/app.py`).
- `ui/components/map_viewer.py`: `plotly_legend_from_codes` (+ `tests/ui/test_map_viewer.py`).

## Batch D — Placeholders / test-only modules

- `validation/checker.py` (`ArchiveChecker`) and `validation/ml_loader.py`
  (`MLLoaderValidator`) plus their `validation/__init__.py` exports and
  `tests/validation/test_validation.py`. **Keep** `validation/gfm.py` (live via
  `atlantis validate-gfm`).
- `harmoniser/tiler.py` (`Tiler`) + export + tests — never instantiated in production.
- `models/metadata.py`: `SourceMetadata` (+ `models/__init__.py` export + tests);
  keep `TileMetadata` (live).
- `fetchers/base.py`: `FloodFetcher` Protocol (test-only).
- `fetchers/registry.py`: `clear_registry` (test-only helper).
- `gfm/__init__.py`: `_build_fetch_result` back-compat alias (+ tests).
- `config.py`: `gfm_api_url`, `log_level` (never read outside tests; CLI logging is
  driven by `--verbose`) + `tests/test_config.py` cases + `docs/gfm/pipeline.md` +
  `src/README.md`.

## Batch E — Docs sync

Remove dangling references to deleted symbols:

- `src/README.md` — planned-modules tables and status notes.
- `docs/archive/zarr-spec.md`, `docs/modis/pipeline.md`, `docs/gfm/pipeline.md`,
  `docs/archive/cog-build.md`.
- `docs/kurosiwo/stac-design.md` only if `stac_api` is touched (planned: `KS_CRS` only).

## Verification

1. Re-run vulture after each batch; the only remaining findings must be entries in
   `vulture_whitelist.py`.
2. `python3 -m compileall` on changed files plus an `rg` sweep for dangling imports.
3. Run the test suite on a machine with the full geo stack (GDAL/HDF4): `pixi run test`
   or `uv run pytest tests/ -m "not e2e"`.
4. Full test execution is **not possible in the Termux shell used for the audit**
   (GDAL/HDF4/geo stack absent). CI is the authoritative gate.

## Follow-ups / constraints

- Adding the dependency requires `uv lock` and `pixi lock` regeneration on a dev
  machine; the audit shell has neither `uv` nor `pixi` on `PATH`.
- Pre-commit excludes `.github/prompts/**` from markdownlint, so this file is not
  linted.

## Open judgment calls

Removal is intentional unless objected to:

- `Tiler`, `aoi_blocks`/`aoi_block_id`, `SourceMetadata`, `gfm_api_url`, `log_level`
  are documented as "planned" or public config. Removing them is a (pre-1.0) API change
  but matches the agreed "dead + test-only" scope.
