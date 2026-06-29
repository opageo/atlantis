# GFM Internals

Developer-facing documentation for the GFM fetcher architecture and processing
pipeline. For usage, see [overview.md](overview.md) and [api.md](api.md).

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      GFMFetcher                         │
│                 (orchestrates the flow)                 │
└─────────────┬───────────────────────┬───────────────────┘
              │                       │
              ▼                       ▼
┌─────────────────────┐    ┌────────────────────────┐
│   Backend Layer     │    │   GfmRasterProcessor   │
│                     │    │                        │
│ • GfmStacBackend    │    │ • Load STAC items      │
│   (EODC STAC)       │    │ • Classified: coarsen, │
│                     │    │   build masks, accum.  │
│ Handles:            │    │ • Native: NN-reproject  │
│ • STAC search       │    │   codes, max-pool      │
│ • Item grouping     │    │ • Write GeoTIFFs       │
└─────────────────────┘    └────────────────────────┘
```

## Current upstream asset scope

The EODC `GFM` collection advertises more assets than Atlantis currently uses.
Today the fetcher loads only:

- `ensemble_flood_extent`
- `reference_water_mask`

Other upstream assets such as `ensemble_water_extent`, `ensemble_likelihood`,
`exclusion_mask`, `advisory_flags`, and the DLR / TUW / LIST intermediate
flood-extent and likelihood layers are not yet exposed through `GFMFetcher`.

This is deliberate in the current pipeline. Atlantis derives its public layers
from the two discrete source assets so the semantics stay stable:
`flood_fraction` comes from flood coverage, `permanent_water` comes from the
permanent-water code, and `quality_mask` records observation coverage only. The
fetcher does not currently reinterpret `advisory_flags` or `exclusion_mask` as
validity masks, because that would change the meaning of `quality_mask` from
"observed" to a mixed coverage-and-confidence signal.

## Processing pipeline

When you run `atlantis fetch --source gfm`, Atlantis executes a date-grouped
SAR pipeline. The processor supports two modes controlled by `classify`
(default `True`):

- **Classified mode** (`--classify`, default): coarsens SAR tiles, builds
  binary masks, reprojects with average resampling, accumulates counts, and
  derives `flood_fraction` / `quality_mask` / `permanent_water`.
- **Native / raw mode** (`--no-classify`): NN-reprojects discrete pixel codes
  directly to the canonical grid and max-pools codes across items for each date;
  emits `ensemble_flood_extent` and `reference_water_mask` as-is.

### End-to-end flow

```mermaid
flowchart TD
    A["User provides bbox and date range"] --> B
    subgraph B["1. Search - GFMFetcher.search()"]
        B1["Query EODC STAC API"]
        B2["Return SearchResult per item"]
        B3["Group items by YYYYMMDD"]
        B1 --> B2 --> B3
    end
    B --> C
    subgraph C["2. Process date group - GfmRasterProcessor.process_items()"]
        C1["odc.stac.load in native CRS"]
        C2["Max-pool by coarsen factor"]
        C3["Build flood, perm, valid masks"]
        C1 --> C2 --> C3
    end
    C --> D
    subgraph D["3. Reproject and accumulate"]
        D1["Reproject to canonical 1-arcmin EPSG:4326 grid"]
        D2["Accumulate flood_count, perm_water_count, valid_count"]
        D1 --> D2
    end
    D --> E
    subgraph E["4. Classify and select"]
        E1["Compute flood_fraction, quality_mask, permanent_water"]
        E2["Apply peak / aggregate / all strategy"]
        E1 --> E2
    end
    E --> F
    subgraph F["5. Package results"]
        F1["Optional processed/ GeoTIFFs"]
        F2["Convert to xarray.Dataset"]
        F1 --> F2
    end
```

### Code trace

Call chain for a single request:

- `GFMFetcher.search()` in `__init__.py` delegates STAC discovery to
  `GfmStacBackend.search()`.
- `GFMFetcher.fetch()` in `__init__.py` groups items by date via
  `GfmStacBackend.group_items_by_date()` and instantiates `GfmRasterProcessor`.
- `GfmRasterProcessor.process_items()` in `processor.py` dispatches to
  `_process_items_classified()` (default) or `_process_items_native()` based on
  the `classify` flag.
- `GFMFetcher._apply_peak_window()` and `_apply_strategy()` in `__init__.py`
  select the final date set and build `FetchResult` objects.
- `processed_tile_to_dataset()` in `dataset.py` converts `GfmProcessedTile`
  into a georeferenced `xarray.Dataset` with either classified or native variables.

## Stage 1 - Search and grouping

`GfmStacBackend` is intentionally small. It wraps the EODC STAC API with three
responsibilities:

| Responsibility         | Implementation                              |
| ---------------------- | ------------------------------------------- |
| STAC endpoint defaults | `DEFAULT_GFM_STAC_URL`, `GFM_COLLECTION_ID` |
| Item search            | `GfmStacBackend.search()`                   |
| Per-date grouping      | `GfmStacBackend.group_items_by_date()`      |

The search step converts the event bbox into a Shapely polygon, queries the
STAC collection, and returns one `SearchResult` per item. Grouping is date-only:
all items with the same `YYYYMMDD` token are processed together.

## Stage 2 - Native load and coarsen (classified mode)

The processor loads each STAC item in its native projected CRS and native
ground sampling distance using `odc.stac.load()`. The first item provides the
source CRS and GSD used for the group. This is still upstream source space,
not Atlantis' canonical 1-arcmin grid.

### Why coarsen first?

Sentinel-1 SAR is noisy at native resolution. In classified mode,
`GfmRasterProcessor` applies a max-pool coarsen step before reprojection:

```python
flood_native = xx["ensemble_flood_extent"].coarsen(...).max()
perm_native = xx["reference_water_mask"].coarsen(...).max()
```

That preserves the flood signal while reducing speckle and runtime. The default
factor is `4`, which turns native ~20 m pixels into an effective ~80 m grid.

In **native / raw mode** this coarsen step is skipped entirely. Each item's
raw uint8 codes are NN-reprojected directly to the canonical 1-arcmin grid and
accumulated via masked-max (`_masked_max()`).

## Stage 3 - Binary masks before reprojection (classified mode only)

GFM uses discrete codes, so classification happens before reprojection. The
processor builds three float32 masks on the coarsened native grid:

| Mask    | Rule                            |
| ------- | ------------------------------- |
| `flood` | `ensemble_flood_extent == 1`    |
| `perm`  | `reference_water_mask == 2`     |
| `valid` | Either source band is not `255` |

This avoids averaging discrete class codes directly. After reprojection with
`average`, each mask becomes a coverage fraction on the output grid.

The important implication is that none of the public Atlantis outputs is a
direct rename of an upstream GFM asset. All three are derived products built
from those binary masks.

## Stage 4 - Canonical-grid reprojection and accumulation (classified mode only)

The processor pre-computes a snapped 1-arcmin destination grid for the event
bbox using `Reprojector._snap_bounds_to_global_grid()`. Every item is then
reprojected onto exactly that grid.

```mermaid
flowchart LR
    A["Native UTM item"] --> B["Coarsen"]
    B --> C["Binary masks"]
    C --> D["Reproject to snapped global grid"]
    D --> E["Accumulate per-pixel counts"]
```

The three count arrays are:

- `flood_count`
- `perm_water_count`
- `valid_count`

Each item contributes a fractional amount in `[0, 1]` to those accumulators.

## Stage 5 - Classification (classified mode only)

`GfmRasterProcessor._classify()` converts the accumulated counts into the final
public layers:

$$
\text{flood\_fraction} = \frac{\text{flood\_count}}{\text{valid\_count}}
$$

with `NaN` where `valid_count == 0`.

$$
\text{quality\_mask} = \mathbb{1}[\text{valid\_count} > 0]
$$

$$
\text{permanent\_water} = \mathbb{1}\left[\frac{\text{perm\_water\_count}}{\text{valid\_count}} > 0.5\right]
$$

`quality_mask` is therefore a valid-observation coverage mask, not the
upstream `advisory_flags` or `exclusion_mask` layer.

`cloud_fraction` is computed as the fraction of pixels with no valid coverage.

## Strategy layer

The fetcher supports the same three top-level strategies exposed in the docs:

| Strategy    | Implementation          | Behavior                                                       |
| ----------- | ----------------------- | -------------------------------------------------------------- |
| `peak`      | `_strategy_peak()`      | Pick the date with the highest `flood_pixel_count()`           |
| `aggregate` | `_strategy_aggregate()` | Mean flood fraction, OR quality, majority-vote permanent water |
| `all`       | `_strategy_all()`       | Keep one `FetchResult` per date                                |

Peak-window filtering and observation subsampling live in
`selection.py`:

- `select_peak_window()` keeps only dates inside a ±N-day window around the peak.
- `subsample_around_peak()` enforces `max_observations` with `post`, `pre`, or
  `balanced` priority.

## Dataset conversion

`processed_tile_to_dataset()` in `dataset.py` converts each `GfmProcessedTile`
into a georeferenced `xarray.Dataset`. It derives pixel-center coordinates from
the affine transform and writes both CRS and transform via `rioxarray`.

The result is the in-memory payload returned through `FetchResult.dataset`.
When `keep_processed=True`, the written `processed/` GeoTIFFs are already on
the same canonical 1-arcmin grid as the in-memory dataset.

## Edge cases

| Scenario                                  | Behavior                                                 |
| ----------------------------------------- | -------------------------------------------------------- |
| No STAC items match the event             | `fetch()` returns `[]` early                             |
| STAC items have no datetime               | `group_items_by_date()` skips them                       |
| A date group produces no valid data       | `process_items()` returns `None` and the date is skipped |
| Tied peak flood counts                    | The earliest date wins                                   |
| Non-date tokens enter peak-window helpers | They are excluded by `_parse_yyyymmdd()`                 |
| Aggregate strategy on a single date       | `aggregate_tiles()` returns that tile unchanged          |
| `--no-classify` with `--plot`             | Plots `ensemble_flood_extent` codes as a raw raster      |
| `--no-classify` with `--harmonise`        | NN-reprojects codes to 1-arcmin; no flood % derivation   |

## Test anchors

- [test_gfm.py](../../tests/fetchers/test_gfm.py) - fetcher defaults, backend wiring,
  registration, and protocol compliance
- [test_gfm_processor.py](../../tests/fetchers/test_gfm_processor.py) - `_classify()`,
  file writing, and aggregate behavior
- [test_gfm_selection.py](../../tests/fetchers/test_gfm_selection.py) - peak-window and
  subsampling logic
- [test_gfm_e2e.py](../../tests/fetchers/test_gfm_e2e.py) - CLI-level reference checks
