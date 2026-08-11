# Plan: Fix GFM Harmonised Validation Issues

## Goal

Make GFM classified and native outputs semantically correct and independently
verifiable against the upstream GFM products. The priority is to eliminate
likelihood corruption, preserve native-band nodata semantics, clarify source
coverage and reference-water provenance, and make the derived fractions
verifiable from their actual inputs.

No implementation should begin until the semantic decisions in this plan are
confirmed, especially the meaning of `exclusion_mask` and the expected
reference-water temporal behavior.

## Confirmed findings

The Valencia validation identified these issues:

1. `ensemble_likelihood` harmonised outputs contain values outside the declared
   `0–100` range, including `156` and `254`.
2. Classified missing likelihood is represented as `uint8(255)`, but the extra
   xarray DataArray does not carry nodata metadata. Harmonisation therefore
   averages `255` as ordinary data.
3. The harmonised writer scales every floating-point DataArray by `100`. Native
   likelihood becomes floating point after average resampling, so it is scaled
   as if it were a physical fraction. This causes uint8 wraparound; for example,
   `255 * 100 mod 256 = 156`.
4. The 2024-10-30 valid source patch is west of the requested AOI. The date has
   no usable SAR coverage inside the requested AOI after the native-coordinate
   load, although its ancillary reference-water asset is populated.
5. `reference_water_mask` is not byte-identical across the selected dates.
   Upstream STAC items use month-specific reference-water asset hrefs, so the
   documented static-baseline assumption must be verified against the GFM
   product contract.
6. `exclusion_mask == 1` frequently overlaps valid fractions. The current
   derivation uses combined flood/water-band validity and does not use the
   companion exclusion mask in `valid_count`.
7. `ensemble_water_extent` is not persisted by the validation run, preventing a
   direct `water_fraction` derivation check.

## Phase 1 — Confirm product semantics upstream

Before changing the pipeline, inspect the authoritative GFM PDD and EODC STAC
metadata/product assets.

### Reference-water provenance

For every Valencia item:

- record the `reference_water_mask` asset href, collection, product version,
  and month/date token;
- compare October and November assets on the same source tile and common
  window;
- determine whether the product contract promises one fixed 2017–2021 mask or
  intentionally supplies month-specific seasonal masks;
- check whether the seasonal class (`2`) is expected to vary by month while the
  permanent class (`1`) remains static.

Decision:

- If the product is static, select/pin a canonical reference asset and add a
  cross-date byte-identity test.
- If it is month-specific by design, update Atlantis documentation and the
  validation criteria instead of forcing byte identity.

### Exclusion semantics

For representative upstream items, compare `exclusion_mask`,
`ensemble_flood_extent`, `ensemble_water_extent`, and `ensemble_likelihood`:

- calculate valid/invalid overlap counts;
- determine whether exclusion means “unobserved” or “observed but unreliable”;
- verify the PDD rule for likelihood inside excluded pixels;
- establish whether excluded pixels must be removed from `valid_count`.

Do not change the fraction denominator until this contract is confirmed.

### Source coverage and AOI behavior

For each item/date:

- transform the AOI into the item CRS;
- record item footprint and transformed AOI bounds;
- calculate valid-pixel bounds for each required native band;
- test whether valid pixels intersect the AOI;
- record dates with no usable in-AOI SAR coverage.

Define whether such dates should be omitted or retained as explicitly marked
all-nodata observations. They must not silently participate in peak selection.

## Phase 2 — Preserve layer metadata

### Target files

- `src/atlantis/fetchers/_dataset.py`
- layer registry/specification code as needed
- related GFM dataset tests

### Changes

When `dataset_from_processed()` builds DataArrays:

- attach the registry-defined `nodata` value;
- preserve dtype and `_FillValue`/`nodata` consistently;
- mark native-code layers separately from physical fractions;
- apply the same behavior to all GFM native extras, not only
  `ensemble_likelihood`.

Expected metadata for native GFM companions includes `uint8` dtype and nodata
`255`.

### Tests

Add tests asserting that `processed_tile_to_dataset()` preserves nodata for:

- `ensemble_likelihood`;
- `exclusion_mask`;
- `advisory_flags`;
- `reference_water`/`reference_water_mask`.

Also verify that missing likelihood values become nodata/NaN during average
reprojection rather than valid values.

## Phase 3 — Make raster serialization layer-aware

### Target file

- `src/atlantis/harmoniser/__init__.py`

### Changes

Refactor `write_harmonised_raster()` so scaling is determined by layer semantics,
not by dtype:

- scale only `flood_fraction` and `water_fraction` from `[0, 1]` to `[0, 100]`;
- write `ensemble_likelihood` unchanged in `[0, 100]`;
- write native masks/codes unchanged;
- write NaN or declared nodata as `255`;
- reject or warn on unknown floating-point native layers rather than silently
  scaling them.

### Regression tests

Verify:

- fraction values `0.0`, `0.5`, `1.0` serialize as `0`, `50`, `100`;
- likelihood values `0`, `50`, `100`, `255` serialize unchanged;
- no likelihood value outside `0–100` is produced except nodata `255`;
- no uint8 wraparound occurs;
- average-resampled likelihood skips nodata subpixels.

This phase and Phase 2 must be implemented together because both are required
to fix the likelihood corruption.

## Phase 4 — Decide and implement exclusion handling

Based on Phase 1:

### If exclusion means unreliable but observed

- keep the documented fraction formula unchanged;
- retain `exclusion_mask` as a quality layer;
- update validation wording so excluded pixels are expected to be flagged, not
  necessarily NaN;
- ensure likelihood is nodata where the PDD requires it.

### If exclusion means unobserved

- include exclusion validity in `valid_count`;
- apply the same mask to flood and water numerators;
- produce NaN fractions where all observations are excluded;
- add invariants and tests for exclusion-driven nodata.

Document the decision in `docs/gfm/`, `docs/layers.md`, and the validation
report/task.

## Phase 5 — Make water derivation verifiable

Persist one of the following during validation/debug runs:

### Preferred

Add native `ensemble_water_extent` to the harmonised/native GeoTIFF and plot
inventory.

### Alternative

Persist diagnostic `ensemble_water_extent_count` and `valid_count` rasters in a
validation-only output mode.

Add direct checks for every valid output pixel:

$$
\text{flood\_fraction} =
\frac{\text{ensemble\_flood\_extent\_count}}{\text{valid\_count}}
$$

$$
\text{water\_fraction} =
\frac{\text{ensemble\_water\_extent\_count}}{\text{valid\_count}}
$$

Required invariants:

- counts are non-negative;
- each numerator is no greater than `valid_count`;
- fractions are within `[0, 1]` internally and `[0, 100]` on disk;
- fractions are nodata exactly where `valid_count == 0`;
- `water_fraction >= flood_fraction` wherever both are valid.

Do not treat the separately generated native `--no-classify` raster as a
byte-exact oracle for classified fractions because the two paths use different
resampling strategies.

## Phase 6 — Add explicit source-coverage diagnostics

Add structured diagnostics to GFM processing/results for each date:

- item count and item IDs;
- valid in-AOI counts for flood and water bands;
- valid in-AOI counts for likelihood and exclusion bands;
- transformed AOI and source footprint;
- whether the date has usable SAR coverage;
- skipped/partially covered item count and reason.

For dates without usable SAR coverage, either:

- omit the date with an explicit warning and diagnostic; or
- retain an all-nodata artifact with an explicit `no_usable_coverage` marker.

Peak selection must ignore dates with no usable flood observations.

Add tests for entirely nodata items, partially overlapping items, and a date
containing both valid and invalid items.

## Phase 7 — Add a compact end-to-end fixture

Create a small local fixture containing:

- valid dry, flood, and water pixels;
- mixed-resolution cells;
- nodata pixels;
- excluded pixels;
- likelihood values near `0`, `50`, and `100`;
- likelihood nodata `255`;
- reference-water codes `0`, `1`, `2`, and `255`.

Test the full path:

1. processed tile;
2. Dataset conversion;
3. harmonisation;
4. GeoTIFF serialization;
5. rasterio reopen;
6. domain, nodata, and derivation invariants.

## Phase 8 — Re-run upstream and Valencia verification

### Upstream verification

Use a read-only Pixi script to:

- query the EODC STAC items for the Valencia AOI;
- record the exact three acquisition dates and all item IDs;
- inspect correctly transformed source windows;
- verify upstream likelihood domain (`0–100` plus `255`);
- verify flood/water/exclusion/l likelihood overlap;
- record reference-water href/version provenance;
- compare source assets with the post-fix outputs.

### Repository validation

From the repository root, run the targeted tests first:

```bash
PYTHONPATH=src pixi run -e default pytest -q \
  tests/fetchers/gfm tests/harmoniser tests/validation

PYTHONPATH=src pixi run -e default ruff check \
  src/atlantis/fetchers/_dataset.py \
  src/atlantis/harmoniser/__init__.py \
  src/atlantis/fetchers/gfm tests/
```

Then rerun the Valencia classified and native fetches, preserving processed
outputs for comparison. Use `--strategy all`, no peak-window filtering, and
persist `ensemble_water_extent` or count diagnostics for the validation run.

Finally rerun all seven validation checks and compare pre-fix/post-fix:

- likelihood domain and nodata behavior;
- 2024-10-30 coverage diagnostics;
- reference-water provenance and date stability;
- exclusion/fraction relationship;
- direct water/flood derivation equations;
- processed versus harmonised grid behavior.

## Acceptance criteria

The fix is complete when:

- likelihood outputs contain only `0–100` and nodata `255`;
- native-code layers are never fraction-scaled;
- missing companion values remain nodata through reprojection and writing;
- 2024-10-30 is either correctly populated from in-AOI source data or clearly
  reported as having no usable in-AOI SAR coverage;
- reference-water temporal behavior matches the confirmed upstream contract;
- exclusion semantics are documented and tested;
- `water_fraction` is directly explainable from persisted/native inputs;
- all fraction domain and nodata invariants pass;
- the validation report has a justified final verdict.

## Suggested issue breakdown

1. Preserve nodata metadata for native GFM companion layers.
2. Prevent fraction scaling of native floating-point layers.
3. Persist `ensemble_water_extent` or GFM diagnostic counts.
4. Confirm and implement GFM exclusion-mask semantics.
5. Resolve monthly versus static reference-water provenance.
6. Add GFM valid-coverage and AOI-intersection diagnostics.
7. Add compact end-to-end GFM serialization fixtures.

Phases 2 and 3 should be delivered together as the critical likelihood
corruption fix. Phases 1 and 4 are semantic gates and should be resolved before
changing fraction validity behavior.
