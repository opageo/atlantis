# Flood Case Metadata: Derivation Notes

## Source

KuroSiwo catalogue (`catalogue.gpkg`) — 1.73M rows, one row per exported SAR patch.
Reference: [github.com/Orion-AI-Lab/KuroSiwo](https://github.com/Orion-AI-Lab/KuroSiwo)

## Delivered table

One row per flood event (43 total, 2015–2022). Fields:

| Column                     | Type      | How derived                                                                                                                                                                              |
| -------------------------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `flood_case`               | str       | `KuroSiwo_{actid:03d}`                                                                                                                                                                   |
| `date_start`               | date      | `flood_date` where `master=False` (pre-flood baseline acquisition)                                                                                                                       |
| `date_end`                 | date      | `flood_date` where `master=True` (flood-time acquisition)                                                                                                                                |
| `lat_min`, `lat_max`       | float (°) | `GeoDataFrame.total_bounds` per event, reprojected to WGS84 (EPSG:4326)                                                                                                                  |
| `lon_min`, `lon_max`       | float (°) | same                                                                                                                                                                                     |
| `max_flood_extent_km2`     | float     | `sum(geom_patch_area_km² × pflood / 100)` over flood-time GRD patches (`crank=1`) with `pflood > 0`, where `geom_patch_area_km²` is computed from geometry in equal-area CRS (EPSG:6933) |
| `date_of_max_flood_extent` | date      | same as `date_end` — KuroSiwo has a single SAR acquisition per event, so peak extent date equals flood date                                                                              |

## Key assumptions

- **Patch area basis**: patch area is geometry-derived in an equal-area CRS (EPSG:6933), i.e., `geometry.area / 1_000_000` after reprojection. This avoids using a fixed nominal patch size and reduces latitude-driven bias.
- **Flood extent estimate**: `max_flood_extent_km2` is a lower-bound approximation. It sums fractional flooded area only where labels exist (`pflood > 0`, non-null), using flood-time GRD patches (`master=True`, `crank=1`). Unlabeled flood-time patches are excluded.
- **Date semantics**: `date_start` / `date_end` reflect SAR acquisition dates, not the real-world flood onset/recession. The temporal gap between them is the pre-flood baseline window, not the flood duration.
- **Bounding box**: derived from all patches of the event (both `master=True` and `master=False`), so it covers the full catalogued AOI, not just the flooded extent.

## Caveats / open questions

- Should extent continue using GRD-only (`crank=1`) for consistency with labeled flood masks, or should SLC (`crank=2`) be incorporated with a separate harmonization rule?
- Should we keep a single global equal-area CRS (`EPSG:6933`) or move to geodesic/per-event area calculation for maximal area fidelity at all latitudes?
- ...
