# Atlantis layers

Auto-generated from the per-source layer registries (`atlantis.fetchers.<source>.layers`). Do not edit by hand — regenerate with `python scripts/generate_layer_docs.py` (or `atlantis list-layers`).

A **native** layer is fetched untouched from the source. A **derived** layer is computed by Atlantis from native inputs (for example `flood_fraction`).

## gfm

### Native layers (gfm)

Layers the source physically provides (fetched untouched).

| Layer                   | dtype | nodata | Resampling | Aggregation | Codes                                                                                | Description                                                                                                                                          |
| ----------------------- | ----- | ------ | ---------- | ----------- | ------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ensemble_flood_extent` | uint8 | 255    | nearest    | max         | `0` = dry / observed-not-flooded; `1` = flood; `255` = nodata                        | Ensemble SAR flood extent, passed through untouched.                                                                                                 |
| `reference_water_mask`  | uint8 | 255    | nearest    | max         | `0` = land; `1` = water (seasonal / observed); `2` = permanent water; `255` = nodata | Reference water mask, passed through untouched. Atlantis follows the EODC COG encoding (2 = permanent), which diverges from the public PDD Table 20. |

### Derived layers (gfm)

Layers Atlantis computes from native inputs (not downloaded).

| Layer             | dtype   | nodata | Inputs                            | Resampling | Aggregation | Description                                                                                            |
| ----------------- | ------- | ------ | --------------------------------- | ---------- | ----------- | ------------------------------------------------------------------------------------------------------ |
| `flood_fraction`  | float32 | None   | `flood_count`, `valid_count`      | average    | nanmean     | Fraction of valid SAR observations flagged as flood (flood_count / valid_count); NaN where unobserved. |
| `quality_mask`    | uint8   | 255    | `valid_count`                     | mode       | any         | Observation-coverage mask: 1 where at least one valid SAR observation contributed.                     |
| `permanent_water` | uint8   | 255    | `perm_water_count`, `valid_count` | mode       | majority    | Permanent-water mask: majority (>50%) of observed coverage is permanent water.                         |

## modis

### Native layers (modis)

Layers the source physically provides (fetched untouched).

| Layer                     | dtype | nodata | Resampling | Aggregation | Codes                                                                                                                           | Description                                                                                                                                                                                      |
| ------------------------- | ----- | ------ | ---------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `raw`                     | uint8 | 255    | nearest    | mode        | `0` = no water; `1` = surface (reference) water; `2` = recurring flood; `3` = unusual flood; `255` = insufficient data / masked | Selected MCDWD flood composite codes, passed through untouched. Resolves via --composite to one of F1 (Flood_1Day_250m), F1C (FloodCS_1Day_250m), F2 (Flood_2Day_250m), or F3 (Flood_3Day_250m). |
| `TotalCounts_1Day_250m`   | uint8 | 255    | average    | mean        |                                                                                                                                 | Potential observations over the 1-day window. Catalogued upstream layer; not loaded by the default pipeline.                                                                                     |
| `TotalCounts_2Day_250m`   | uint8 | 255    | average    | mean        |                                                                                                                                 | Potential observations over the 2-day window. Catalogued upstream layer; not loaded by the default pipeline.                                                                                     |
| `TotalCounts_3Day_250m`   | uint8 | 255    | average    | mean        |                                                                                                                                 | Potential observations over the 3-day window. Catalogued upstream layer; not loaded by the default pipeline.                                                                                     |
| `ValidCountsCS_1Day_250m` | uint8 | 255    | average    | mean        |                                                                                                                                 | Clear-sky observations (cloud-shadow screened), 1-day. Catalogued upstream layer; not loaded by the default pipeline.                                                                            |
| `ValidCounts_1Day_250m`   | uint8 | 255    | average    | mean        |                                                                                                                                 | Clear-sky observations, 1-day. Catalogued upstream layer; not loaded by the default pipeline.                                                                                                    |
| `ValidCounts_2Day_250m`   | uint8 | 255    | average    | mean        |                                                                                                                                 | Clear-sky observations, 2-day. Catalogued upstream layer; not loaded by the default pipeline.                                                                                                    |
| `ValidCounts_3Day_250m`   | uint8 | 255    | average    | mean        |                                                                                                                                 | Clear-sky observations, 3-day. Catalogued upstream layer; not loaded by the default pipeline.                                                                                                    |
| `WaterCountsCS_1Day_250m` | uint8 | 255    | average    | mean        |                                                                                                                                 | Water detections (terrain + cloud-shadow masked), 1-day. Catalogued upstream layer; not loaded by the default pipeline.                                                                          |
| `WaterCounts_1Day_250m`   | uint8 | 255    | average    | mean        |                                                                                                                                 | Water detections (terrain masked), 1-day. Catalogued upstream layer; not loaded by the default pipeline.                                                                                         |
| `WaterCounts_2Day_250m`   | uint8 | 255    | average    | mean        |                                                                                                                                 | Water detections, 2-day. Catalogued upstream layer; not loaded by the default pipeline.                                                                                                          |
| `WaterCounts_3Day_250m`   | uint8 | 255    | average    | mean        |                                                                                                                                 | Water detections, 3-day. Catalogued upstream layer; not loaded by the default pipeline.                                                                                                          |

### Derived layers (modis)

Layers Atlantis computes from native inputs (not downloaded).

| Layer             | dtype   | nodata | Inputs | Resampling | Aggregation | Description                                                                                                                                                            |
| ----------------- | ------- | ------ | ------ | ---------- | ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `flood_fraction`  | float32 | None   | `raw`  | average    | nanmean     | Binary unusual-flood flag (composite == 3) as float32; insufficient-data pixels (255) are NaN so the harmoniser's averaging yields a sub-pixel fraction.               |
| `quality_mask`    | uint8   | 0      | `raw`  | mode       | mode        | Valid-observation mask (composite != 255). 1 = usable classification, 0 = insufficient data (always HAND/terrain-shadow masked; cloud handling is composite-specific). |
| `permanent_water` | uint8   | 0      | `raw`  | mode       | mode        | Reference surface-water mask (composite == 1).                                                                                                                         |
| `recurring_flood` | uint8   | 0      | `raw`  | mode       | mode        | MODIS-only recurring-flood mask (composite == 2).                                                                                                                      |

## viirs

### Native layers (viirs)

Layers the source physically provides (fetched untouched).

| Layer | dtype | nodata | Resampling | Aggregation | Codes                                                                                                                                                                                                                                                                                           | Description                                                                                                                                                                                                                                                                                                                              |
| ----- | ----- | ------ | ---------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `raw` | uint8 | 1      | nearest    | mode        | `1` = no_valid_data (source fill); `15` = floodwater without fraction retrieval; `16` = bareland; `17` = vegetation; `20` = snow_ice; `27` = river/lake ice; `30` = cloud; `38` = super-snow/ice water or mixed ice & water or melting ice; `50` = shadow; `99` = normal water (NOAA reference) | Single encoded VIIRS flood band (NOAA VFM), passed through untouched. Codes 100-200 encode water fraction as (code - 100)%; other codes are land-cover, cloud, or water classes (see codes). The source \_FillValue is 1 (no_valid_data); Atlantis also treats 0 (clip/mosaic fill) as missing and writes its raw GeoTIFF with nodata=0. |

### Derived layers (viirs)

Layers Atlantis computes from native inputs (not downloaded).

| Layer             | dtype   | nodata | Inputs | Resampling | Aggregation | Description                                                                                                                                                                      |
| ----------------- | ------- | ------ | ------ | ---------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `flood_fraction`  | float32 | None   | `raw`  | average    | nanmean     | Continuous water fraction decoded from codes 101-200 as (code-100)/100. Valid non-flood observations map to 0.0; fill and cloud pixels are NaN so temporal averaging skips them. |
| `quality_mask`    | uint8   | 0      | `raw`  | mode       | mode        | Valid clear-sky observation mask: 0 for fill (0, 1) or cloud (30), 1 otherwise. Pre-existing water classes count as valid observations.                                          |
| `permanent_water` | uint8   | 0      | `raw`  | mode       | mode        | Reference (NormalWater, code 99) permanent-water mask.                                                                                                                           |
| `cloud_mask`      | uint8   | 0      | `raw`  | mode       | mode        | Cloud mask (code 30): 1 where the pixel is cloud-covered.                                                                                                                        |
| `snow_ice`        | uint8   | 0      | `raw`  | mode       | mode        | Snow/ice mask (NOAA code 20).                                                                                                                                                    |
| `shadow`          | uint8   | 0      | `raw`  | mode       | mode        | Terrain/cloud shadow mask (code 50) — flags low-confidence observations.                                                                                                         |
