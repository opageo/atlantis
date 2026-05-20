# Flood Case Metadata: Derivation Notes

## Source

KuroSiwo catalogue (`catalogue.gpkg`) — 1.73M rows, one row per exported SAR patch.
Reference: [github.com/Orion-AI-Lab/KuroSiwo](https://github.com/Orion-AI-Lab/KuroSiwo)

## Data model terminology

| Term                          | Definition                                                                                                  |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------- |
| **Event** (`actid`)           | A single flood case. 43 events total (2015–2022).                                                           |
| **Tile** (`grid_id`)          | A unique 256×256 px spatial location. Each tile belongs to exactly one event. An event has 22–43,349 tiles. |
| **Patch** (= 1 catalogue row) | A tile observed at one time × one product type. Exported to disk as a `.tif`.                               |
| **`pflood`**                  | Ground-truth flood label (0–100%) per tile. Static across time — same for pre-flood and flood-time patches. |
| **`master`**                  | Temporal role: `True` = flood-time, `False` = pre-flood baseline.                                           |
| **`crank`**                   | Product: `1` = GRD (amplitude), `2` = SLC (complex).                                                        |

**Hierarchy:** Event → Tiles → Patches (1–3 per tile: pre-flood GRD, pre-flood SLC, flood-time GRD)

**Spatial note:** Tile sizes are NOT uniform (1.3–4.9 km², CV=33%) because SAR
ground-range pixel size varies with latitude and incidence angle. All area calculations
use geometry-derived areas in EPSG:6933 (equal-area), not a fixed nominal tile size.

## Field availability summary

One row per flood event (43 total, 2015–2022).

### Minimum required fields

| Field          | Status       | How it is derived                                                                                                                                                          |
| -------------- | ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `flood_case`   | ✓ Available  | `actid` column, formatted as `KuroSiwo_{actid:03d}`                                                                                                                        |
| `date_start`   | ⚠ Proxy only | Earliest `source_date` where `master=False` — oldest of typically 2 pre-flood SAR acquisitions per event; marks the start of the observational window, not the flood onset |
| `date_end`     | ⚠ Proxy only | `source_date` where `master=True` — typically 1 flood-time SAR acquisition per event; the date the flood was imaged, not necessarily peak or end of flood                  |
| `bounding_box` | ✓ Available  | `GeoDataFrame.total_bounds` per event, reprojected to WGS84 (EPSG:4326); covers the SAR tile footprint                                                                     |

### Extra fields

| Field                      | Status          | How it is derived                                                                                                    |
| -------------------------- | --------------- | -------------------------------------------------------------------------------------------------------------------- |
| `max_flood_extent_km²`     | ⚠ Approximation | `sum(pflood_fraction × patch_area_km²)` over flood-time GRD patches with `pflood > 0`, patch area in EPSG:6933       |
| `date_of_max_flood_extent` | ✗ Not derivable | Set equal to `date_end` — only one flood-time acquisition exists per event, so peak-extent date cannot be determined |

## Delivered table

One row per flood event (43 total, 2015–2022). Full derivation details:

| Column                     | Type      | How derived                                                                                                                                                                              |
| -------------------------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `flood_case`               | str       | `KuroSiwo_{actid:03d}`                                                                                                                                                                   |
| `date_start`               | date      | Earliest `source_date` where `master=False` — oldest of typically 2 pre-flood SAR acquisitions (40/43 events have exactly 2; a few have 3 or 4)                                          |
| `date_end`                 | date      | `source_date` where `master=True` — typically 1 flood-time SAR acquisition per event (40/43 events); earliest used where 2 exist                                                         |
| `lat_min`, `lat_max`       | float (°) | `GeoDataFrame.total_bounds` per event, reprojected to WGS84 (EPSG:4326)                                                                                                                  |
| `lon_min`, `lon_max`       | float (°) | same                                                                                                                                                                                     |
| `max_flood_extent_km2`     | float     | `sum(geom_patch_area_km² × pflood / 100)` over flood-time GRD patches (`crank=1`) with `pflood > 0`, where `geom_patch_area_km²` is computed from geometry in equal-area CRS (EPSG:6933) |
| `date_of_max_flood_extent` | date      | Set equal to `date_end` — KuroSiwo provides only one flood-time SAR acquisition per event, so true peak-extent date is not determinable from this dataset (see Limitations)              |

## Critical finding: `pflood` is a static spatial label

**`pflood` is identical for pre-flood and flood-time acquisitions in all 43 events.**
It encodes the ground-truth flood extent per tile, applied uniformly to all temporal
acquisitions as an ML training label. It does **not** represent a per-acquisition
flood signal.

- `pflood` is valid for computing **spatial flood extent** (how much area is labelled as flooded)
- `pflood` **cannot** be used to assess flood signal intensity per acquisition date
- Pre-flood images carry the same `pflood` as flood-time — by design for change-detection ML
  (the model learns to detect SAR change between pre and post, with `pflood` as the shared target)

## Key assumptions

- **Patch area basis**: patch area is geometry-derived in an equal-area CRS (EPSG:6933), i.e., `geometry.area / 1_000_000` after reprojection. This avoids using a fixed nominal patch size and reduces latitude-driven bias.
- **Flood extent estimate**: `max_flood_extent_km2` is a lower-bound approximation. It sums fractional flooded area only where labels exist (`pflood > 0`, non-null), using flood-time GRD patches (`master=True`, `crank=1`). Unlabeled flood-time patches are excluded.
- **Date semantics**: `date_start` and `date_end` are SAR acquisition dates (`source_date`), not
  the real-world flood onset/recession. `date_start` is the oldest pre-flood baseline image date;
  `date_end` is the flood-time image date. Neither bounds the actual hydrological event.
  Note: the catalogue also contains a `flood_date` column (nominal event date, constant per event)
  which was intentionally **not** used — it produces identical `date_start` and `date_end` for all events.
- **Bounding box**: reflects the SAR tile footprint for the event AOI, not the inundated extent. This is standard for SAR-derived products — the box is derived from the union of all catalogued patches (both `master=True` and `master=False`) and should not be interpreted as a flood extent proxy.

## Limitations

1. **No flood timeline.** Each event has exactly one pre-flood baseline and one flood-time SAR acquisition. There is no multi-temporal stack, so flood growth, peak, or recession cannot be tracked.
2. **Flood onset and end dates are unknown.** `date_start` and `date_end` are SAR image dates, not hydrological event boundaries. The actual start and end of flooding are not recorded in the catalogue.
3. **`date_of_max_flood_extent` cannot be verified.** With only one flood-time acquisition per event, it is impossible to confirm that the image captured peak inundation. For some events the acquisition may have occurred during flood recession.
4. **`max_flood_extent_km2` is patch-level, not pixel-level.** The `pflood` label is a per-patch percentage (256×256 px tiles); sub-patch spatial variability is averaged out. This introduces smoothing error, especially for events with heterogeneous inundation patterns.
5. **Large temporal gaps.** 10/43 events have >6-month gaps between the last pre-flood and first flood-time acquisition; 2 events exceed 1 year (max: 672 days, Event 1111008). Long baselines may degrade change-detection performance due to land-cover change unrelated to flooding.
6. **`pflood` is a static label, not a temporal signal.** It is identical across pre-flood and flood-time acquisitions (verified for all 43 events). It cannot be used to distinguish flood signal strength per image — only for spatial extent estimation.

## Open questions

- Should extent continue using GRD-only (`crank=1`) for consistency with labeled flood masks, or should SLC (`crank=2`) be incorporated with a separate harmonization rule?
- Should we keep a single global equal-area CRS (`EPSG:6933`) or move to geodesic/per-event area calculation for maximal area fidelity at all latitudes?
