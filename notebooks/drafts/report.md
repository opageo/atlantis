# Flood Case Metadata: Derivation Notes

## Source

KuroSiwo catalogue (`catalogue.gpkg`) — 1.73M rows, one row per exported SAR patch.  
Reference: [github.com/Orion-AI-Lab/KuroSiwo](https://github.com/Orion-AI-Lab/KuroSiwo)

## Delivered table

One row per flood event (43 total, 2015–2022). Fields:

| Column | Type | How derived |
|--------|------|-------------|
| `flood_case` | str | `KuroSiwo_{actid:03d}` |
| `date_start` | date | `flood_date` where `master=False` (pre-flood baseline acquisition) |
| `date_end` | date | `flood_date` where `master=True` (flood-time acquisition) |
| `lat_min`, `lat_max` | float (°) | `GeoDataFrame.total_bounds` per event, reprojected to WGS84 (EPSG:4326) |
| `lon_min`, `lon_max` | float (°) | same |
| `max_flood_extent_km2` | float | `sum(patch_area_km² × pflood / 100)` over flood-time patches with `pflood > 0` |
| `date_of_max_flood_extent` | date | same as `date_end` — KuroSiwo has a single SAR acquisition per event, so peak extent date equals flood date |

## Key assumptions

- **Patch area**: 256 × 256 pixels at 10 m/px → **6.554 km²** per patch. This is the nominal Sentinel-1 GRD resolution; actual ground coverage may vary slightly at range edges.
- **Flood extent estimate**: `max_flood_extent_km2` is a lower-bound approximation. It sums the fractional flooded area per labeled patch (`pflood > 0`). Only 11% of flood-time patches carry a `pflood` label; unlabeled patches are excluded.
- **Date semantics**: `date_start` / `date_end` reflect SAR acquisition dates, not the real-world flood onset/recession. The temporal gap between them is the pre-flood baseline window, not the flood duration.
- **Bounding box**: derived from all patches of the event (both `master=True` and `master=False`), so it covers the full catalogued AOI, not just the flooded extent.

## Caveats / open questions

- Should `max_flood_extent_km2` be restricted to `crank=1` (GRD) patches only, or include SLC (`crank=2`) as well? Currently only GRD is used for consistency with the labeled subset.
- ...