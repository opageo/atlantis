# Atlantis layers

This is the **canonical human-readable layer inventory** for Atlantis.
Other docs should link here instead of repeating native/derived layer tables.

Auto-generated from the per-source layer registries (`atlantis.fetchers.<source>.layers`). Do not edit by hand — regenerate with `python scripts/generate_layer_docs.py` (or `atlantis list-layers`).

A **native** layer is fetched untouched from the source. A **derived** layer is computed by Atlantis from native inputs (for example `flood_fraction`).

## Quick links

- `gfm`: [native](#layers-gfm-native) / [derived](#layers-gfm-derived)
- `modis`: [native](#layers-modis-native) / [derived](#layers-modis-derived)
- `viirs`: [native](#layers-viirs-native) / [derived](#layers-viirs-derived)

<a id="layers-gfm"></a>
## gfm

> GFM native extent bands are converted to derived fractions through the following recipe: build 0/1 masks at native resolution from the extent bands, mean-pool by the coarsen factor, average-reproject to the ~80 m processed grid, then accumulate per-class counts across the date group. `water_fraction` / `flood_fraction` are the class count divided by `valid_count` (NaN where unobserved); `reference_water` is the masked-max of native reference-water codes.

<a id="layers-gfm-native"></a>
### Native layers (gfm)

Layers the source physically provides (fetched untouched).

| Layer | dtype | nodata | Resampling | Aggregation | Codes | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `ensemble_flood_extent` | uint8 | 255 | nearest | masked_max | `0` = dry / observed-not-flooded; `1` = flood; `255` = nodata | Ensemble SAR flood extent, passed through untouched. |
| `ensemble_water_extent` | uint8 | 255 | nearest | masked_max | `0` = dry / observed-not-water; `1` = water; `255` = nodata | Ensemble SAR water extent, passed through untouched. |
| `reference_water_mask` | uint8 | 255 | nearest | masked_max | `0` = no water; `1` = permanent water; `2` = seasonal water; `255` = nodata | Reference water mask, passed through untouched. Codes follow GFM PDD Table 20: 0 = no water, 1 = permanent water, 2 = seasonal water. The seasonal class (2) is the GFM analog of MODIS recurring_flood. |
| `exclusion_mask` | uint8 | 255 | nearest | masked_max |  | Native GFM exclusion-mask codes, passed through untouched. |
| `ensemble_likelihood` | uint8 | 255 | average | masked_max |  | Native GFM ensemble flood-likelihood values (0-100), passed through untouched. |
| `advisory_flags` | uint8 | 255 | nearest | masked_or |  | Native GFM advisory bitmask codes, passed through untouched. |

<a id="layers-gfm-derived"></a>
### Derived layers (gfm)

Layers Atlantis computes from native inputs (not downloaded).

| Layer | dtype | nodata | Inputs | Resampling | Aggregation | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `water_fraction` | float32 | None | `ensemble_water_extent_count`, `valid_count` | average | nanmean | Fraction of valid SAR observations flagged as water (ensemble_water_extent_count / valid_count); ensemble_water_extent_count is accumulated from native ensemble_water_extent, and valid_count from the combined per-pixel validity of ensemble_flood_extent, ensemble_water_extent, and reference_water_mask, across the date group; NaN where unobserved. |
| `flood_fraction` | float32 | None | `ensemble_flood_extent_count`, `valid_count` | average | nanmean | Fraction of valid SAR observations flagged as flood (ensemble_flood_extent_count / valid_count); ensemble_flood_extent_count is accumulated from native ensemble_flood_extent, and valid_count from the combined per-pixel validity of ensemble_flood_extent, ensemble_water_extent, and reference_water_mask, across the date group; NaN where unobserved. |
| `reference_water` | uint8 | 255 | `reference_water_mask_codes` | nearest | masked_max | Reference-water codes carried through from native reference_water_mask (masked-max across the date group) under the shared layer name. |

<a id="layers-modis"></a>
## modis

<a id="layers-modis-native"></a>
### Native layers (modis)

Layers the source physically provides (fetched untouched).

| Layer | dtype | nodata | Resampling | Aggregation | Codes | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `raw` | uint8 | 255 | nearest | mode | `0` = no water; `1` = surface (reference) water; `2` = recurring flood; `3` = unusual flood; `255` = insufficient data / masked | Selected MCDWD flood composite codes, passed through untouched. Resolves via --composite to one of F1 (Flood_1Day_250m), F1C (FloodCS_1Day_250m), F2 (Flood_2Day_250m), or F3 (Flood_3Day_250m). |
| `TotalCounts_1Day_250m` | uint8 | 255 | average | mean |  | Potential observations over the 1-day window. Catalogued upstream layer; not loaded by the default pipeline. |
| `TotalCounts_2Day_250m` | uint8 | 255 | average | mean |  | Potential observations over the 2-day window. Catalogued upstream layer; not loaded by the default pipeline. |
| `TotalCounts_3Day_250m` | uint8 | 255 | average | mean |  | Potential observations over the 3-day window. Catalogued upstream layer; not loaded by the default pipeline. |
| `ValidCountsCS_1Day_250m` | uint8 | 255 | average | mean |  | Clear-sky observations (cloud-shadow screened), 1-day. Catalogued upstream layer; not loaded by the default pipeline. |
| `ValidCounts_1Day_250m` | uint8 | 255 | average | mean |  | Clear-sky observations, 1-day. Catalogued upstream layer; not loaded by the default pipeline. |
| `ValidCounts_2Day_250m` | uint8 | 255 | average | mean |  | Clear-sky observations, 2-day. Catalogued upstream layer; not loaded by the default pipeline. |
| `ValidCounts_3Day_250m` | uint8 | 255 | average | mean |  | Clear-sky observations, 3-day. Catalogued upstream layer; not loaded by the default pipeline. |
| `WaterCountsCS_1Day_250m` | uint8 | 255 | average | mean |  | Water detections (terrain + cloud-shadow masked), 1-day. Catalogued upstream layer; not loaded by the default pipeline. |
| `WaterCounts_1Day_250m` | uint8 | 255 | average | mean |  | Water detections (terrain masked), 1-day. Catalogued upstream layer; not loaded by the default pipeline. |
| `WaterCounts_2Day_250m` | uint8 | 255 | average | mean |  | Water detections, 2-day. Catalogued upstream layer; not loaded by the default pipeline. |
| `WaterCounts_3Day_250m` | uint8 | 255 | average | mean |  | Water detections, 3-day. Catalogued upstream layer; not loaded by the default pipeline. |

<a id="layers-modis-derived"></a>
### Derived layers (modis)

Layers Atlantis computes from native inputs (not downloaded).

| Layer | dtype | nodata | Inputs | Resampling | Aggregation | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `water_fraction` | float32 | None | `raw` | average | nanmean | Binary water-observation fraction from classes 1/2/3 as float32; insufficient-data pixels (255) are NaN so downstream averaging yields a sub-pixel fraction. |
| `flood_fraction` | float32 | None | `raw` | average | nanmean | Binary unusual-flood flag (composite == 3) as float32; insufficient-data pixels (255) are NaN so the harmoniser's averaging yields a sub-pixel fraction. |
| `exclusion_mask` | uint8 | 0 | `raw` | mode | mode | Exclusion / insufficient-data mask (composite == 255). 1 = excluded or invalid, 0 = usable classification. |
| `reference_water` | uint8 | 0 | `raw` | mode | mode | Reference water mask (surface water or recurring flood: classes 1 and 2). |
| `recurring_flood` | uint8 | 0 | `raw` | mode | mode | MODIS-only recurring-flood mask (composite == 2). |

<a id="layers-viirs"></a>
## viirs

<a id="layers-viirs-native"></a>
### Native layers (viirs)

Layers the source physically provides (fetched untouched).

| Layer | dtype | nodata | Resampling | Aggregation | Codes | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `raw` | uint8 | 1 | nearest | mode | `1` = no_valid_data (source fill); `15` = floodwater without fraction retrieval; `16` = bareland; `17` = vegetation; `20` = snow_ice; `27` = river/lake ice; `30` = cloud; `38` = super-snow/ice water or mixed ice & water or melting ice; `50` = shadow; `99` = normal water (NOAA reference) | Single encoded VIIRS flood band (NOAA VFM), passed through untouched. Codes 100-200 encode water fraction as (code - 100)%; other codes are land-cover, cloud, or water classes (see codes). The source _FillValue is 1 (no_valid_data); Atlantis also treats 0 (clip/mosaic fill) as missing and writes its raw GeoTIFF with nodata=0. |

<a id="layers-viirs-derived"></a>
### Derived layers (viirs)

Layers Atlantis computes from native inputs (not downloaded).

| Layer | dtype | nodata | Inputs | Resampling | Aggregation | Description |
| --- | --- | --- | --- | --- | --- | --- |
| `water_fraction` | float32 | None | `raw` | average | nanmean | Continuous water fraction decoded from codes 100-200 as (code-100)/100, with NOAA reference-water code 99 and unquantified floodwater code 15 forced to 1.0. Fill and cloud pixels are NaN so temporal averaging skips them. |
| `flood_fraction` | float32 | None | `raw` | average | nanmean | Continuous fraction decoded directly from the NOAA 100-200 fraction codes. Reference-water code 99 and unquantified floodwater code 15 remain 0.0 here; fill and cloud pixels are NaN so temporal averaging skips them. |
| `exclusion_mask` | uint8 | 0 | `raw` | mode | all_true | Exclusion mask: 1 for fill (0, 1) or cloud (30), 0 otherwise. Pre-existing water classes count as usable observations. |
| `reference_water` | uint8 | 0 | `raw` | mode | majority | Reference-water mask for NOAA NormalWater (code 99). |
| `cloud_mask` | uint8 | 0 | `raw` | mode | mode | Cloud mask (code 30): 1 where the pixel is cloud-covered. |
| `snow_ice` | uint8 | 0 | `raw` | mode | mode | Snow/ice mask (NOAA code 20). |
| `shadow` | uint8 | 0 | `raw` | mode | mode | Terrain/cloud shadow mask (code 50) — flags low-confidence observations. |

<a id="layers-cross-source"></a>
## Cross-source gotchas

The per-source sections above describe each source in isolation. The hazards below only matter when GFM, MODIS, and VIIRS outputs are stitched together (unioned, compared, or averaged in one pipeline).

### `reference_water` differs in both schema and nodata

- **GFM** `reference_water` is a carried-through **3-class** native band (`0` = no water, `1` = permanent, `2` = seasonal), `nodata=255`.
- **MODIS** `reference_water` is a **binary** `0/1` mask (classes `1` and `2`), `nodata=0`.
- **VIIRS** `reference_water` is a **binary** `0/1` mask (code `99`), `nodata=0`.

Code that unions or compares `reference_water` masks across sources must handle `255` vs `0` explicitly **and** must not collapse GFM's permanent/seasonal split into a single bit.

### `0` in `reference_water` means different things across sources

The nodata/`0` encoding is not a shared data-availability convention:

- **GFM** — `255` genuinely means "unobserved"; `0` means "observed, no water". The two are distinguishable from the raster alone.
- **MODIS / VIIRS** — `nodata=0` is a shared **rendering convention** for all binary derived masks (background renders transparent), **not** a data-availability flag. A pixel that could not be observed (MODIS insufficient-data `255`; VIIRS fill/cloud codes `0`/`1`/`30`) is also written as `0` in `reference_water` — indistinguishable from genuinely-observed non-water. On a single date (or `peak` strategy) you must pair `reference_water` with `exclusion_mask` (`1` = fill/cloud/insufficient) to tell "observed non-water" from "couldn't observe". In `aggregate` mode this is partly mitigated because the masks are reduced over non-excluded dates only (VIIRS `majority`, MODIS `mode`).

### `exclusion_mask` is a binary mask for MODIS/VIIRS but native codes for GFM

- **MODIS / VIIRS** `exclusion_mask` is a clean **binary `0/1`** mask (`0` = usable, `1` = excluded), `nodata=0`.
- **GFM** `exclusion_mask` is **native multi-valued GFM codes** (`nodata=255`), passed through untouched — not a binary `0/1` mask.

Averaging or OR-ing `exclusion_mask` across sources yields garbage on the GFM side; convert GFM codes to a binary mask before combining.

### Aggregation policies differ by source

| Layer | gfm | modis | viirs |
| --- | --- | --- | --- |
| `exclusion_mask` | masked_max | mode | all_true |
| `reference_water` | masked_max | mode | majority |
| `flood_fraction` / `water_fraction` | nanmean | nanmean | nanmean |

An aggregate pipeline that mixes sources applies different per-source logic to the masks. In particular VIIRS is conservative — a pixel is excluded only if **every** observation was fill/cloud, and `reference_water` requires a strict majority of usable observations — while MODIS reduces over all dates. See the per-source operator values in the table above.

### `reference_water` semantics differ

GFM and MODIS both carry a permanent-vs-seasonal split, but VIIRS does not:

- **GFM** — permanent (`1`) and seasonal (`2`); the seasonal class is the GFM analog of MODIS `recurring_flood`.
- **MODIS** — `recurring_flood` is a separate derived layer (class `2`); `reference_water` itself folds classes `1` and `2` together.
- **VIIRS** — only a single "normal water" class (`99`); no permanent/seasonal split exists.

Recurring/seasonal water is therefore only available from MODIS and GFM, not from VIIRS.
