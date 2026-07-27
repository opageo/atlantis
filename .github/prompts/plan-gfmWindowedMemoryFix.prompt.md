# Plan: GFM windowed native processing (builds on the merged Phase C fix in plan-gfmPeakMemoryFix.prompt.md)

**Tracking:** GitHub issue #96 (already open — do not open a new one).

**STATUS (2026-07-27, session update): Phase W.0/W.1 done. Phase W.2 (implementation +
correctness gate) is IN PROGRESS, NOT PASSING — do not proceed to W.3/W.4/W.6.** A major
coarsen-phase-misalignment bug (not anticipated anywhere in this plan's original risk analysis)
was found and fixed, closing most of the gap, but a real, localized residual discrepancy remains
on `water_fraction` (~0.08% of pixels, max diff ~0.43). `GfmRasterProcessor(window_size=...)` is
implemented and unit-tested for its pure helpers, but is marked EXPERIMENTAL in code (a runtime
warning fires whenever it's used) and defaults to `None` (today's unwindowed behaviour, fully
unchanged and verified via the full test suite). See "Phase W.2 implementation + correctness gate"
in `/memories/repo/gfm-investigation.md` for the full writeup, root-cause evidence, and concrete
next steps for whoever continues this.

## TL;DR

`plan-gfmPeakMemoryFix.prompt.md`'s Phase C fix (split the per-item
`odc.stac.load` into two band groups + coarsen each native mask sequentially,
[processor.py](../../src/atlantis/fetchers/gfm/processor.py)) is merged,
verified, and safe: measured per-cell peak dropped from **~15.0 GiB → ~11.0
GiB** (~27%) on the same real STAC item used throughout this investigation.
That is still well above VIIRS/MODIS-class memory (~4GB), and the residual
~11 GiB is inherent to processing a full ~15000×15000 native EQUI7 tile
**eagerly, all at once** — no more load-reordering trick is left to pull.

This plan's fix is **windowed native processing**: instead of loading and
processing an item's full native tile in one shot, split it into a grid of
smaller pixel-aligned native windows, run the existing
load → mask → coarsen → reproject pipeline **once per window**, and
accumulate each window's contribution into the canonical output grid — which
is already small (~4722×6091 px in the reference tile, ~115 MB per float32
band) regardless of windowing, since only the _native, pre-reprojection_
stages are the memory problem (confirmed in Phase C.1). Each item's several
STAC-item accumulation loop already merges partial-coverage contributions via
additive counts and masked-max codes
(`atlantis.fetchers.gfm.processor._masked_max` /
`GfmRasterProcessor._process_items_classified`) — a spatial window is,
mathematically, just another partial-coverage contribution to the same
accumulators, so **no new accumulation math is needed**, only a new outer
loop dimension (window) alongside the existing one (item).

**The one real risk this plan is built around, and the reason it front-loads
a correctness gate before a memory gate:** a window boundary that doesn't
align exactly with the existing `coarsen(...).mean()` step
(`GfmRasterProcessor._build_native_masks`) can silently corrupt data at
window seams — either double-counting pixels (data biased at seams) or
dropping them (small systematic undercount at seams) — without an
OOM/crash to flag it. A silent, regularly-gridded correctness bug in flood
data is a worse outcome than the OOM this plan is meant to fix. Every phase
below is ordered so correctness is proven _before_ any memory number is
trusted, mirroring the discipline that worked in the superseded plan
(measure before implementing, don't ship an unverified hypothesis).

## Confirmed coordinates and facts (already discovered — do not re-discover)

- Reference test item (same one used throughout Phase C.1/C.2, keep reusing
  it for apples-to-apples comparisons): `ENSEMBLE_FLOOD_20241101T060232_VV_EU020M_E036N009T3`,
  Equi7 tile `EU020M_E036N009T3`, found via
  `Client.open(DEFAULT_GFM_STAC_URL).search(collections=GFM_COLLECTION_ID, bbox=(-1.5, 38.8, 0.5, 40.0), datetime=(2024-10-29, 2024-11-04))`.
- **Confirmed real STAC properties for this item** (queried directly, metadata-only, no data read — cheap and safe to repeat):
  - `proj:shape`: `[15000, 15000]` — GFM Equi7 T3 tiles are a **fixed native
    pixel size**, not variable per item. Every task in the real 205,635-row
    backlog should share this same native shape (Equi7 grid is fixed).
  - `proj:transform`: `[20, 0, 3600000, 0, -20, 1200000]` — native pixel size
    20 m, origin at `(3600000, 1200000)` in the item's own projected CRS
    (`proj:wkt2`, an Azimuthal Equidistant projection centered on Europe —
    this is the Equi7 "EU" continental grid, not a generic UTM zone).
  - `gsd`: `20` (metres), matches `proj:transform`.
- **`_load_item`'s existing `bbox=aoi.bounds` parameter to `odc.stac.load`
  already does windowed/clipped reads** — `aoi` is a `shapely.box` built from
  whatever bbox is passed in, in EPSG:4326. Confirmed by reading
  [processor.py](../../src/atlantis/fetchers/gfm/processor.py)'s
  `_load_item`: nothing about it assumes the AOI is the whole tile. This
  means the _plumbing_ for windowed loads already exists and needs zero
  changes — only the **caller** (`_process_items_classified`) needs to loop
  over a grid of smaller AOIs instead of one tile-wide AOI.
- Measured baseline to beat: **~11.0 GiB peak** (post Phase C fix), harness
  at [`scripts/profile_gfm_peak_memory.py`](../../scripts/profile_gfm_peak_memory.py)
  (a tracked, reusable script — not a committed pytest test — reuse it, extend
  it, don't rewrite it from scratch).
- Default `coarsen_factor` is `4` (`DEFAULT_COARSEN_FACTOR` in
  [processor.py](../../src/atlantis/fetchers/gfm/processor.py)), overridable
  via `--gfm-coarsen-factor` / `ATLANTIS_GFM_COARSEN_FACTOR`. **Window size
  must be chosen as a multiple of whatever `coarsen_factor` is in effect** —
  see Phase W.1.

## Risk assessment and mitigations

A dedicated look at what could realistically go wrong, in order of how badly
it hurts if it happens — not just the risks this plan's steps already
mention in passing.

### R.1 — Silent seam corruption from bbox-snapping (CRITICAL — the risk the whole plan is ordered around)

- **What could go wrong**: `odc.stac.load(bbox=...)` will rasterize the
  requested EPSG:4326 bbox onto the source's native pixel grid by snapping to
  its pixel edges. Unless the window bboxes passed to it are computed so the
  snapping is exact (down to floating-point rounding, not half a pixel), an
  interior window edge can silently shift by a native pixel or two in either
  direction — meaning a native pixel row/column near a seam gets counted
  either twice (once in each adjacent window) or zero times. Flood/water
  fractions are means over counts, so a small seam-corruption wouldn't crash
  or show up as an obviously-wrong number — it would be a small, _regularly
  spatially-gridded_ bias invisible to spot-checking a handful of pixels.
- **Mitigation**: (1) Compute each window's bbox by transforming its exact
  native pixel-index edges through the item's own `proj:transform` (the
  already-confirmed `[20, 0, 3600000, 0, -20, 1200000]`, not a
  re-derived estimate) into the source CRS, then into EPSG:4326 — never by
  subdividing the lon/lat `self.bbox` directly (that's exactly why Phase
  W.1's Option A is recommended over Option B, not a stylistic preference).
  (2) Unit-test the bbox-conversion helper by _simulating the snap_: assert
  `odc.stac.load` (or an equivalent rasterio `transform_bounds`/window
  round-trip) maps each computed window bbox back to its intended pixel
  range exactly, on synthetic transforms — don't only test the forward
  conversion. (3) Phase W.2's golden-reference gate is the final,
  empirical backstop (an exact or near-exact match across a full real
  15000×15000 tile at multiple window sizes is strong evidence no seam is
  leaking pixels) — but it must not be the _only_ defence, since it depends
  on the reference item being representative.

### R.2 — `proj:transform`/`proj:shape` missing or untrustworthy on some of the 205,635 real rows

- **What could go wrong**: the plan's Option A assumes every real GFM STAC
  item carries a reliable `proj:transform` + `proj:shape`, confirmed for one
  sample item but never sampled across the full real catalogue. If a subset
  of items (different year, different Equi7 continent, older acquisition
  scheme) lacks these properties or has subtly different native
  shape/transform than the confirmed `15000×15000` sample, a windowing
  implementation that assumes the sample's values could misalign windows for
  those items — reintroducing R.1 at scale on exactly the rows no one looked
  at.
- **Mitigation**: (1) In Phase W.1, before writing pipeline code, sample the
  real catalogue (`s3://atlantis/assets/gfm/gfm_archive_catalog_2025.parquet`)
  or the STAC API across years and Equi7 continents and confirm
  `proj:shape`/`proj:transform` are consistently present and the
  `15000×15000` shape is actually universal — record the sample size and
  result in `/memories/repo/gfm-investigation.md`, not just an assertion.
  (2) The implementation must **derive windows from each item's own
  metadata**, never hardcode `15000×15000` or the sample transform — the
  helper's inputs are the per-item `proj:*` values, so a legitimately
  different (but valid) shape still works. (3) Fail loudly (raise, log,
  skip the item with a warning) if `proj:transform`/`proj:shape` are missing
  on a given item, rather than falling back to a guess — a loud failure on
  an edge-case item is recoverable; a silent misalignment is not.

### R.3 — Massive `odc.stac.load` request-count amplification at 205,635-cell scale

- **What could go wrong**: windowing multiplies the number of
  `odc.stac.load` calls per item (currently 2, from Phase C's band split) by
  the number of windows per item. At, say, 3000 px windows (5×5 = 25 windows)
  that's 25–50 `odc.stac.load` calls per item instead of 2, each carrying
  COG header parsing, GDAL dataset-open cost, and HTTP range-request latency
  (see Phase W.3.2). Across the full 205,635-cell backlog, that's millions
  more HTTP requests against EODC's object storage than the current design
  makes — a risk to (a) wall-clock throughput of the whole backlog, and (b)
  whether EODC's object storage rate-limits, degrades, or becomes flaky
  under a much higher small-request volume, turning the backlog run into a
  slow, retry-heavy grind (and the Phase C `_retry_read` budget is tuned for
  the current request volume, not this amplified one).
- **Mitigation**: (1) Phase W.3's measurement gate already records
  wall-clock + request count, not just peak RSS — treat a >2–3× wall-clock
  regression at the chosen window size as a blocker, not a footnote, and
  prefer a larger window that still meets the memory target (see W.3.3).
  (2) Phase W.3.4's re-evaluation of the band-group split exists precisely
  for this — collapsing back to 1 `odc.stac.load` per window (all 6 bands at
  once, if memory allows) halves request count; decide it with numbers.
  (3) Flag explicitly in Phase W.6 that a small-scale validation run (Phase
  D.1 of the superseded plan) must watch for elevated retry rates in
  `_retry_read` logs as a canary for object-storage stress at scale, before
  committing to the full 205,635-cell run — don't discover it 10% into the
  real backlog. (4) Note as a further-consideration fallback: if request
  amplification is unacceptable at scale, window only the two costly stages
  (`_load_item` + `_build_native_masks`) but batch reprojections across
  windows (reprojection was measured ~free in Phase C.1).

### R.4 — Float-tolerance gate too loose → a real seam bug passes as "float noise"

- **What could go wrong**: Phase W.2's correctness gate compares windowed
  output to the golden reference "within a tight, explicit float tolerance."
  If that tolerance is set by guessing (e.g. `1e-6`) without understanding
  what legitimate reduction-order noise _should_ look like, it can be
  accidentally set loose enough to mask the exact small, regular seam bias
  R.1 is worried about — the gate looks green while a real bug ships.
- **Mitigation**: (1) In Phase W.0, establish the tolerance empirically,
  not by fiat: run the _same_ unwindowed pipeline twice with nothing changed
  and diff — that's a lower bound on "legitimate" noise (should be ~0 or
  near-machine-precision for identical runs). Any windowed-vs-unwindowed
  diff _meaningfully_ above that baseline needs an explanation, not a
  looser tolerance. (2) Require the diff to be **spatially structureless**:
  a legitimate float-reduction-order diff is tiny and randomly distributed;
  R.1's seam bug shows up as a diff concentrated along the window grid lines
  at a regular pixel spacing — check for this explicitly (e.g. histogram the
  per-pixel abs-diff; if the max diff sits on window boundaries, that's a
  bug, not noise). (3) Document the actual tolerance and rationale in the
  test that uses it (Phase W.5.2), so a future reader can tell whether it
  was measured or guessed.

### R.5 — Golden reference itself unrepresentative or stale

- **What could go wrong**: Phase W.0's golden reference is one (ideally two)
  real item(s). If the windowing change is later exercised on a cell whose
  data distribution or item-count differs materially from the reference
  (e.g. a cell near the Equi7 continental edge, a much higher item-count
  ascending/descending overlap), a subtle correctness difference could exist
  that the reference-based gate never exercised. Similarly, if the golden
  reference is regenerated casually mid-plan (e.g. after an unrelated
  upstream change), a real regression could be "absorbed" into the new
  baseline unnoticed.
- **Mitigation**: (1) Generate the reference **once**, save it with the
  exact code commit SHA it was produced from recorded alongside it, and
  treat any intentional regeneration as a flagged decision, not a routine
  step. (2) The two-cell minimum in Phase W.0.2 (single-item + multi-item)
  is a hard requirement, not a "nice to have" — the multi-item accumulation
  path is the least-covered, highest-risk interaction with windowing.
  (3) If Phase W.6's real-scale validation surfaces cells with materially
  different shapes/item counts than the reference, spot-check one such cell
  against a one-off unwindowed run of it (the unwindowed path is preserved
  behind the Phase W.2 flag precisely so it stays available as an oracle,
  not deleted).

### R.6 — Option B (lon/lat tiling) silently abandoned but its risk unretired

- **What could go wrong**: Phase W.1 lists Option B as a documented fallback.
  If Option A hits a snag mid-implementation and someone switches to Option B
  opportunistically, the lon/lat → native pixel alignment risk (R.1, via a
  different mechanism: a lon/lat window edge is not guaranteed to fall on a
  native pixel multiple of `coarsen_factor` after reprojection back to
  source space) comes back _without_ Option A's mitigations, since Option B
  has no equivalent "exact native pixel-edge" anchor.
- **Mitigation**: switching to Option B is a **plan-level decision requiring
  explicit sign-off, not an implementation detail** — record it as a
  decision in `/memories/repo/gfm-investigation.md` with the reason, and
  re-run the full Phase W.2 correctness gate against the same golden
  reference under Option B before treating its memory numbers as meaningful
  (Option B gets no "trust it because Option A passed" credit).

### R.7 — Windowing interacts badly with `_retry_read`'s retry budget at scale

- **What could go wrong**: `_retry_read` retries a failed `odc.stac.load` a
  bounded number of times before skipping the item. With windowing, a
  transient EODC failure now fails (and retries) _one window_ instead of the
  whole item — mostly good (finer-grained failure isolation), but it also
  means a flakier-than-usual network stretch now generates far more retry
  traffic (every window retries independently), and an item whose windows
  _partially_ fail can silently end up with a systematically
  spatially-incomplete contribution (some windows skipped, others
  processed), which is a subtler version of R.1's spatial-bias problem
  (not a crash, just missing data in a regular pattern).
- **Mitigation**: (1) When a window's `_load_item` returns `None` after
  exhausting retries, log it at a level that will actually be noticed
  (`logger.warning`, already the case) and count/track per-item skipped
  windows — an item that dropped some of its windows should be visibly
  flagged in logs/metrics, not silently produce a slightly-sparser result.
  (2) Phase W.6's small-scale validation should include at least one
  stretch where retries actually fire (they did in the Phase C test runs —
  EODC's object storage does emit transient 404/500s), confirming the
  window-level failure path behaves sanely, not just the happy path.

## Steps

### Phase W.0 — Golden-reference baseline _(no code beyond a throwaway script; do first, blocks everything else)_

1. Using the current merged (post Phase C) code, run the full classified
   pipeline for the reference item end-to-end and save the output arrays
   (`water_fraction`, `flood_fraction`, `reference_water`, `exclusion_mask`,
   `advisory_flags`, `ensemble_likelihood` — everything in
   `GfmProcessedTile`) to a local `.npz` file. This is the **golden
   reference** every subsequent windowed-processing change must reproduce
   (within float tolerance) before its memory numbers are allowed to matter.
   **Record the exact git commit SHA it was produced from alongside it, and
   treat any later regeneration as a flagged decision, not a routine step**
   (R.5 — otherwise a real regression can be silently absorbed into a
   casually-refreshed baseline).
2. Do this for at least the reference item alone, **and** a second real
   `(date, equi7_tile)` cell that has **more than one STAC item** sharing the
   cell (ascending + descending Sentinel-1 passes on the same day — the
   multi-item accumulation path is a second thing windowing must not break,
   distinct from the single-item case already profiled). This is a hard
   requirement, not a "nice to have" (R.5). Search the real catalogue
   (`s3://atlantis/assets/gfm/gfm_archive_catalog_2025.parquet`) or the STAC
   API directly to find one.
3. **Establish the float tolerance empirically, not by fiat** (R.4): run the
   same unwindowed pipeline twice with nothing changed and diff the outputs —
   that's the lower bound for "legitimate" reduction-order noise (expected:
   ~0 or near machine precision). Any windowed-vs-unwindowed diff _above_
   this baseline in Phase W.2 needs an explanation, not a looser tolerance.
4. Keep the golden-reference script in `scripts/` (tracked, so it stays
   available across sessions/clones) and its generated `.npz` output under
   `scripts/data/` (gitignored, same convention as other generated/large data
   in this repo) — not `tmp/` — e.g. `scripts/data/gfm_golden_reference_single.npz`
   - `scripts/save_gfm_golden_reference.py`.

### Phase W.1 — Design decision: how windows are defined _(no code yet — decide and write down the answer before implementing)_

This is the crux of the correctness risk (R.1). Two options, in increasing
order of implementation cost and decreasing order of risk:

**Option A — Pixel-aligned native windows (recommended).** Using the item's
own `proj:transform` + `proj:shape` (confirmed above — cheap metadata already
on the STAC item, no data read needed), define windows as row/col pixel-index
ranges of the native `15000×15000` grid, convert each window's pixel-index
range to a native-CRS bbox via `proj:transform`, then to an EPSG:4326 bbox
(via `pyproj.Transformer`) to pass as `aoi.bounds` into the existing
`_load_item(..., bbox=...)` call. **Hard constraint**: window size (in native
pixels) must itself be an exact multiple of `coarsen_factor`, so
`_build_native_masks`'s `.coarsen(..., boundary="trim")` never has to trim a
partial block _inside_ a window — trimming must only ever happen (if at all)
at the tile's true outer edge, exactly like today's non-windowed behaviour,
never at an interior window seam (R.1). Concretely, for the default
`coarsen_factor=4` and confirmed `proj:shape=[15000, 15000]`: valid window
sizes include `3000` px (5×5 grid, `3000/4=750` exact), `1500` px (10×10
grid), or `5000` px (3×3 grid) — **not** `3750` (`3750/4=937.5`, would trim
inconsistently at interior seams). Write a small pure helper (easy to unit
test) that validates this constraint and raises rather than silently
producing misaligned windows if a future `coarsen_factor` change makes the
chosen window size invalid.

**Before writing pipeline code, also do these two data-grounding checks
(R.2)** — cheap, metadata-only, no data reads:

- Sample the real catalogue (`s3://atlantis/assets/gfm/gfm_archive_catalog_2025.parquet`)
  or the STAC API across multiple years and Equi7 continents and confirm
  `proj:shape`/`proj:transform` are consistently present and the
  `15000×15000` shape is actually universal — record sample size + result
  in `/memories/repo/gfm-investigation.md`.
- Confirm the plan does **not** hardcode `15000×15000` anywhere — the
  windowing helpers must derive everything from each item's own per-item
  metadata, and **fail loudly** (raise/skip with a visible warning) if
  `proj:transform`/`proj:shape` are missing on any given item, never guess
  (R.2).

**Option B — Lon/lat bbox tiling (documented fallback only — see R.6).**
Split the existing lon/lat `self.bbox` into an N×M grid directly (no
pixel-index math), reusing `_load_item`'s current `aoi.bounds` contract
unchanged. Simpler code, but the window edges are defined in the
_destination_ (reprojected) coordinate space rather than the _source_ native
pixel grid, so alignment with `coarsen_factor`-multiple boundaries in the
source's native pixel space is only approximate — there is no guarantee a
lon/lat-defined window boundary falls exactly on a native pixel multiple of
`coarsen_factor` after reprojection back to source space, reintroducing the
interior-seam-trim risk Option A explicitly avoids (R.1). **Switching to
Option B is a plan-level decision requiring explicit sign-off, not an
implementation detail** (R.6): record it in
`/memories/repo/gfm-investigation.md` with the reason, and re-run the full
Phase W.2 correctness gate under Option B before treating its memory numbers
as meaningful — Option B gets no "trust it because Option A passed" credit.

**Decide and record which option is used, and the chosen window size(s) to
try, before writing any pipeline code** — this is a "measure before
implementing" gate, same discipline as Phase C.1.

### Phase W.2 — Implement windowed processing behind a flag, validate correctness first _(mandatory gate before any memory measurement)_

1. Add a pure, unit-testable helper (e.g. `_native_pixel_windows(shape, window_size, coarsen_factor) -> list[window]`)
   that, given `proj:shape` and a chosen window size, yields non-overlapping,
   gap-free pixel-index windows tiling the full native grid, validating the
   `coarsen_factor` divisibility constraint from Phase W.1. Unit test this in
   isolation first (no STAC/network needed) — assert full coverage (union of
   windows == full grid, no gaps), no overlaps, and that it raises on an
   invalid (window_size, coarsen_factor) combination.
2. Add a helper to convert one pixel-index window + `proj:transform` +
   `proj:wkt2` into an EPSG:4326 bbox suitable for `_load_item`'s existing
   `aoi.bounds` parameter (pure coordinate math, also unit-testable without
   network). **Unit-test the round-trip, not just the forward conversion**
   (R.1): simulate `odc.stac.load`'s pixel-edge snapping (or an equivalent
   `rasterio.transform_bounds`/window round-trip) on synthetic transforms and
   assert each computed window bbox snaps back to its intended pixel range
   exactly — a forward-conversion test alone doesn't catch a half-pixel
   drift at a seam.
3. Restructure `GfmRasterProcessor._process_items_classified`'s per-item loop
   into a nested `for item in items: for window in windows(item): ...` —
   reusing the _exact same_ body (load → `_build_native_masks` → reproject →
   accumulate) that already runs per-item today, changing only what `aoi` is
   passed to `_load_item` on each inner iteration. The existing accumulation
   variables (`flood_count`, `water_count`, `valid_count`,
   `reference_water_codes`, `exclusion_codes`, `advisory_flags`,
   `ensemble_likelihood`) and their existing merge functions (`+=`,
   `_masked_max`, `_masked_or`, `np.fmax`) do not need to change — a window
   is accumulated exactly like an extra item would be, since both are
   partial-coverage contributions to the same canonical-grid accumulators.
   Gate this behind a constructor parameter (e.g.
   `GfmRasterProcessor(..., window_size: int | None = None)`, `None` =
   today's unwindowed behaviour) rather than replacing the existing path
   outright, so Phase W.3's correctness comparison is a same-process A/B, not
   a before/after-a-deploy comparison — and so the unwindowed path stays
   available as an oracle for future spot-checks (R.5).
4. **Correctness gate — do not proceed to Phase W.3 until this passes**: run
   the _same_ reference item(s) from Phase W.0 through the new windowed path
   at 2-3 different window sizes (e.g. `3000`, `1500`, and one more) and
   assert the output matches the Phase W.0 golden reference within the
   tolerance established empirically in Phase W.0 step 3 (R.4). **Also check
   the diff is spatially structureless** (R.4): a legitimate
   float-reduction-order diff is tiny and randomly distributed; a seam bug
   shows up as a diff concentrated along the window grid lines at a regular
   pixel spacing — histogram the per-pixel abs-diff; if the max diff sits on
   window boundaries, that's a bug, not noise. Do this for both the
   single-item reference cell and the multi-item cell from Phase W.0 step 2
   — the multi-item + multi-window interaction (each item independently
   windowed, then accumulated together) is exactly the kind of interaction
   most likely to hide a subtle bug.

### Phase W.3 — Memory re-measurement _(mandatory gate before picking a production default)_

1. Extend [`scripts/profile_gfm_peak_memory.py`](../../scripts/profile_gfm_peak_memory.py)
   (or add a sibling script — keep both disposable) to accept a window size
   parameter and report peak RSS the same way (via
   `resource.getrusage(RUSAGE_SELF).ru_maxrss`, matching every prior
   measurement in this investigation so numbers stay comparable).
2. Measure peak RSS for the reference item at each window size validated in
   Phase W.2, and record: (a) peak RSS, (b) wall-clock time for the whole
   item, (c) number of `odc.stac.load` calls made (windows × band-groups).
   Expect diminishing memory returns and _growing_ per-call overhead (COG
   header parsing, STAC/GDAL dataset-open cost, network round-trip latency)
   as window count increases — this is a real trade-off, not just "smaller
   is always better." Report both axes, don't optimize memory alone.
   **Treat a >2–3× wall-clock regression at a candidate window size as a
   blocker, not a footnote** (R.3) — at 205,635-cell scale, wall-clock is
   not free.
3. Pick a default window size that comfortably beats the ~11 GiB Phase C
   baseline (target: VIIRS/MODIS-class ~4GB or lower) without an
   unreasonable wall-clock/request-count regression — define "unreasonable"
   relative to the measured single-window (unwindowed) wall-clock time for
   the same item, not an arbitrary number.
4. **Re-evaluate whether the existing `_CLASSIFIED_MASK_BANDS` /
   `_CLASSIFIED_CODE_BANDS` split (from the merged Phase C fix) is still
   worth keeping once windowing is in place.** If a window is already small
   enough that loading all 6 `GFM_BANDS` in one call per window comfortably
   fits the memory target, simplifying back to one `odc.stac.load` per
   window (instead of two) would roughly halve the request count from Phase
   W.3 step 2's measurement — a real win given windowing's own request-count
   cost (R.3). Decide with numbers, not by presumption; keep both split and
   unsplit variants measurable so this is a data-driven choice, not a
   guess repeating the mistake the superseded plan made.

### Phase W.4 — Pick and wire in the production default

1. Based on Phase W.3's numbers, set the default `window_size` (and decide
   the band-group question from W.3.4) as the new default behaviour of
   `GfmRasterProcessor` for the classified path (`_process_items_native` is
   out of scope here — not the current backlog blocker, no evidence it has
   the same problem; flag as a possible future defensive check only, don't
   change it speculatively).
2. Decide whether the window size should be a hardcoded constant (simplest,
   matches how `_CLASSIFIED_MASK_BANDS` is a constant today) or configurable
   via an env var / CLI flag mirroring the `ATLANTIS_GFM_COARSEN_FACTOR`
   pattern (more flexible, more surface area) — default to the simpler
   hardcoded-constant option unless Phase W.3's numbers show real hosts need
   different tuning (e.g. a memory-constrained host wanting smaller windows
   at a throughput cost).
3. Update the docstrings this plan's predecessor already added pointing at
   this future work — `GfmRasterProcessor._build_native_masks`'s docstring
   note in [processor.py](../../src/atlantis/fetchers/gfm/processor.py) and
   the "GFM memory, further work" callout in
   [docs/archive/cube-build.md](../../docs/archive/cube-build.md) — replace
   "not attempted yet" with what was actually built and the new measured
   peak.

### Phase W.5 — Regression tests

1. Unit tests (no network) for the pure helpers from Phase W.2 step 1-2:
   full-coverage/no-gap/no-overlap window tiling, the `coarsen_factor`
   divisibility guard (including that it raises on an invalid combination),
   the pixel-window → EPSG:4326 bbox coordinate conversion, **and the
   bbox → pixel-range round-trip** from Phase W.2 step 2 (R.1 — the seam
   risk's cheapest, most targeted guard).
2. A correctness regression test using a synthetic multi-band raster (extend
   [`tests/fetchers/gfm/test_processor_memory.py`](../../tests/fetchers/gfm/test_processor_memory.py)
   or add a sibling) that processes the **same synthetic data** once
   unwindowed and once windowed (e.g. 2×2 or 4×4 grid over a small synthetic
   tile sized so it cleanly divides), and asserts the two results match
   within the tolerance established in Phase W.0 step 3 (R.4) — this is the
   permanent, fast, offline version of Phase W.2's real-item correctness
   gate, and the single most important test this plan adds (it's the
   regression guard against silently reintroducing a seam bug later).
   Document the tolerance and where it came from in the test, not just as a
   magic number.
3. Extend the existing "load is split into band groups" test if the Phase
   W.3.4 decision keeps the split; update/remove it if windowing made the
   split unnecessary and it was simplified away.
4. Run the full suite in both environments per repo convention:
   `uv run pytest` and `pixi run -e batch pytest --ignore=tests/ui` (the
   `nicegui`-requiring `ui` tests are a known pre-existing gap in the
   `batch` pixi feature, unrelated to this work — see
   `/memories/repo/testing.md`).

### Phase W.6 — Re-validate at real scale, refresh guidance, reconsider Phase D

1. Re-run the disposable harness end-to-end at full scale (real item, chosen
   window size) one more time after all of Phase W.2-W.5 land, to get a
   final, post-merge peak number to document (should match Phase W.3's
   number, but confirms nothing regressed while wiring in W.4/W.5).
2. Update `docs/archive/cube-build.md`'s GFM `--memory-limit` guidance again
   with the new number (currently says ~11-14 GB from the Phase C fix).
3. **Reconsider Phase D from the superseded plan** (small validation run,
   then the real 205,635-cell backlog run against
   `s3://atlantis/assets/gfm/gfm_archive_catalog_2025.parquet` /
   `s3://atlantis/zarr/2025` / `s3://atlantis/db/archive_tracker_gfm_2025.db`)
   now that per-cell peak may be VIIRS/MODIS-class — this was explicitly
   deferred in the Phase C session pending this work. Do not start the real
   backlog run without a fresh small-scale validation pass first (same
   discipline as the superseded plan's Phase D.1), and during that pass
   **explicitly watch `_retry_read` retry rates in the logs as a canary for
   object-storage stress from the request-count amplification** (R.3/R.7) —
   don't discover it 10% into the real backlog run. Include at least one
   stretch where retries actually fire (they did in the Phase C test runs —
   EODC's object storage does emit transient 404/500s) to confirm the
   window-level failure path behaves sanely, not just the happy path (R.7).
   If validation surfaces cells with materially different shapes/item counts
   than the Phase W.0 reference, spot-check one such cell against a one-off
   unwindowed run of it (the unwindowed path is preserved behind the Phase
   W.2 flag precisely so it stays available as an oracle — R.5).

## Relevant files

- [`src/atlantis/fetchers/gfm/processor.py`](../../src/atlantis/fetchers/gfm/processor.py) —
  `GfmRasterProcessor.__init__` (grid/transform setup), `_load_item` (already
  accepts an arbitrary `aoi`/`bands` — no change needed to its contract),
  `_process_items_classified` (the nested item/window loop goes here),
  `_build_native_masks` (the `coarsen_factor`-alignment constraint this plan
  is built around), `_CLASSIFIED_MASK_BANDS` / `_CLASSIFIED_CODE_BANDS` (Phase
  C's band split — Phase W.3.4 decides its fate), `_reproject_*_to_canonical_grid`
  (unchanged; already reprojects onto the fixed, small canonical grid
  regardless of window).
- [`scripts/profile_gfm_peak_memory.py`](../../scripts/profile_gfm_peak_memory.py) —
  existing disposable profiling harness; extend for Phase W.3, don't rewrite.
- [`tests/fetchers/gfm/test_processor_memory.py`](../../tests/fetchers/gfm/test_processor_memory.py) —
  Phase C's regression test (band-group split + coarse RSS ceiling); Phase
  W.5 extends this file with the windowing correctness test.
- [`docs/archive/cube-build.md`](../../docs/archive/cube-build.md) — the
  `--memory-limit` guidance row + "GFM memory, further work" callout Phase
  W.4/W.6 update.
- `/memories/repo/gfm-investigation.md` — the running investigation log;
  keep appending here (Phase C.1/C.2 results already recorded), not a new
  memory file.

## Verification

1. Phase W.0: golden-reference `.npz` exists, was generated from a recorded
   git SHA (R.5), and is reviewed as sane (no NaNs where there shouldn't be,
   cloud_fraction/shape as expected) before anything is compared against it;
   the empirical float-tolerance baseline (Phase W.0 step 3, R.4) is
   measured and recorded.
2. Phase W.1: the chosen window-definition option (A or B) and window size(s)
   to try are written down (in this plan file or repo memory) before any
   pipeline code is written; the catalogue `proj:*` sampling check (R.2) is
   done and recorded.
3. Phase W.2: windowed output matches the Phase W.0 golden reference within
   the documented tolerance, for **both** the single-item and multi-item
   reference cells, at **all** window sizes tried — and the per-pixel diff is
   confirmed spatially structureless (no concentration along window
   boundaries, R.4) — this gate blocks Phase W.3 entirely; a memory win
   that comes with a correctness regression is not an acceptable trade in
   this codebase (flood data feeding downstream decisions).
4. Phase W.3: a table of (window size → peak RSS, wall-clock, request count)
   exists and a default is chosen from it with a stated rationale, not a
   guess; the wall-clock guardrail (R.3) is applied when picking the default.
5. Phase W.4/W.5: full suite green in both `uv run pytest` and
   `pixi run -e batch pytest --ignore=tests/ui`; new unit tests for the
   window-tiling helpers (including the bbox round-trip, R.1) pass; the
   windowing correctness regression test (Phase W.5.2) passes and is
   committed (this is the test that would have caught a seam bug, so it must
   actually run in CI, not just have run once locally).
6. Phase W.6: final real-item peak number documented in both
   `docs/archive/cube-build.md` and `/memories/repo/gfm-investigation.md`;
   the `_retry_read` retry-rate canary (R.3/R.7) was watched during the
   small-scale validation pass; explicit decision recorded on whether Phase
   D (real backlog run) is now in scope.

## Decisions

- This plan is **additive** to the merged Phase C fix, not a replacement —
  the band-group split and sequential-coarsen changes stay regardless of
  what Phase W.3.4 decides about band-grouping _within_ a window.
- Correctness-first ordering (Phase W.0 → W.2 gate → W.3 measurement) is
  deliberate: this plan's core risk (R.1, silent seam corruption) is worse
  than the OOM it fixes, so no memory number is trusted until the
  golden-reference comparison passes.
- Option A (pixel-aligned native windows, Phase W.1) is the recommended
  starting point over Option B (lon/lat tiling) specifically because the real
  reference item already confirms `proj:transform`/`proj:shape` are present
  and cheap to read — Option B is a documented fallback only, not a
  co-equal choice (R.6), unless the catalogue sample from Phase W.1's
  data-grounding check (R.2) shows those properties are unreliable.
- `_process_items_native` (the non-classified/raw path) is explicitly out of
  scope — no evidence it shares the classified path's peak-memory problem,
  and speculatively changing it would violate the "don't fix what isn't
  proven broken" discipline this whole investigation has followed.
- No new GitHub issue — continues to track under #96 per the user's standing
  preference from the Phase C session.

## Further Considerations

1. R.3's fallback if request-count/wall-clock overhead turns out worse than
   expected even after Phase W.3.4's band-group simplification: window only
   the two costliest stages (`_load_item` + `_build_native_masks`) at a
   small window size, but batch multiple windows' reprojection calls before
   accumulating (reprojection was measured as ~0 cost in Phase C.1, so it's
   not obviously worth windowing on its own) — not designed here, only
   flagged as a fallback if the straightforward per-window full pipeline
   turns out to be too slow at 205,635-cell scale.
2. If Phase W.6's small-scale validation shows EODC's object storage
   degrading under the higher request volume (R.3/R.7) beyond what a
   generous `_retry_read` budget can absorb, a small per-worker HTTP
   connection/retry tuning pass may be warranted — out of scope for this
   plan, flagged for whoever runs Phase D.
3. Once this plan's fix is in and validated, the two-stage historical
   narrative (Phase C's band-split fix, then this plan's windowing fix) is
   worth condensing into a single clean summary in
   `/memories/repo/gfm-investigation.md` for future readers, rather than
   requiring them to read both plans' full history to understand the current
   state.
