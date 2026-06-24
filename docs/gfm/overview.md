# GFM Flood Detection

**SAR-based flood mapping from Sentinel-1**

Atlantis integrates the **Global Flood Monitor (GFM)** — a near-real-time flood
extent product derived from Sentinel-1 SAR imagery, operated by the EODC
(Earth Observation Data Centre). Unlike optical products (e.g. VIIRS), SAR
penetrates cloud cover, making GFM reliable during the heavy rainfall that
typically accompanies flood events.

For CLI flags, processing details, output formats, and implementation notes,
see [pipeline.md](pipeline.md). For Python usage, see [api.md](api.md). For the
implementation flow, see [internals.md](internals.md).

## What is GFM?

GFM produces daily flood extent maps by detecting changes in SAR backscatter
from Sentinel-1A and Sentinel-1B. Two key bands are provided per acquisition:

| Asset                   | Meaning                                                                                |
| ----------------------- | -------------------------------------------------------------------------------------- |
| `ensemble_flood_extent` | Flood classification: 0 = dry, 1 = flood, 255 = nodata                                 |
| `reference_water_mask`  | Water type: 0 = land, 1 = water (seasonal/observed), 2 = permanent water, 255 = nodata |

Atlantis currently loads only those two upstream assets. The EODC STAC
collection publishes additional GFM layers as well, but Atlantis does not yet
expose arbitrary asset selection or a raw passthrough mode for GFM.

Native product resolution is **~20 m** in the STAC COGs. Atlantis coarsens to
an effective ~80 m intermediate grid (default `--gfm-coarsen-factor 4`) before
reprojecting onto its canonical 1-arcmin EPSG:4326 output grid.

Data is accessed via the **EODC STAC API** (`https://stac.eodc.eu/api/v1`,
collection `GFM`) using Cloud-Optimised GeoTIFFs — no separate download step
is required.

## How Atlantis processes GFM

```mermaid
flowchart LR
  A["Search EODC STAC"] --> B["Group items by date"]
  B --> C["Load native SAR scenes"]
  C --> D["Coarsen and build masks"]
  D --> E["Reproject to canonical grid"]
  E --> F["Select peak, aggregate, or all"]
```

GFM is the best fit when cloud cover would hide flood signal in optical data.
Atlantis keeps the user-facing workflow similar to VIIRS and MODIS, but the
sensor-specific processing is SAR-first: coarsen, mask, reproject, then
accumulate.

## Comparison with VIIRS / MODIS

GFM, VIIRS, and MODIS share the same Atlantis pipeline surface — identical
`search()` / `fetch()` / `to_dataset()` methods, the same three strategies
(`peak`, `aggregate`, `all`), and the same peak-window / subsampling API
(`peak_days_before`, `peak_days_after`, `max_observations`, `peak_priority`).

Key differences:

| Aspect                         | VIIRS / MODIS                                                                 | GFM                                                                                                                        |
| ------------------------------ | ----------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| **Source assets loaded**       | Native (375 m / 250 m, EPSG:4326)                                             | Native ~20 m projected COGs from EODC STAC (`ensemble_flood_extent`, `reference_water_mask`)                               |
| **Atlantis processed grid**    | Native resolution/grid unless harmonised                                      | Canonical 1-arcmin EPSG:4326 during fetch                                                                                  |
| **`--harmonise` effect**       | Reprojects to 1-arcmin + encodes uint8 %                                      | Re-encodes processed `flood_fraction` float32 [0,1] → uint8 [0,100] on the **same** grid at default settings               |
| **Raw mode (`--no-classify`)** | Available — writes raw integer pixel codes                                    | Not available — Atlantis always derives `flood_fraction`, `quality_mask`, and `permanent_water` from the two source assets |
| **Stream / download toggle**   | `--stream` / `--no-stream`                                                    | Always streamed via `odc.stac`; the shared `--no-stream` flag does not change GFM behavior                                 |
| **Setup requirement**          | VIIRS needs `uv run python scripts/setup.py` for the AOI grid                 | None — data accessed directly via the EODC STAC API                                                                        |
| **Python default `strategy`**  | `"peak"`                                                                      | `"peak"`                                                                                                                   |
| **Dataset variables**          | `flood_fraction` (float32), `quality_mask` (uint8), `permanent_water` (uint8) | Same names, same dtypes                                                                                                    |
| **Harmonised output**          | uint8 [0–100], nodata=255, canonical 1-arcmin grid                            | Same format — stackable with VIIRS / MODIS without resampling                                                              |

Atlantis fetches native GFM source COGs, but the written `processed/` outputs
are already on the canonical 1-arcmin grid. The harmonised TIFF is therefore a
re-encoded flood layer, not a second spatial reprojection under default
settings.

Because both GFM `processed/` output and the harmonised GeoTIFF sit on that
canonical grid, GFM and VIIRS harmonised outputs over the same AOI are
**directly stackable** without any further resampling.

## Demo script

```bash
# Arbitrary bbox + date range — Valencia 2024 flood
uv run python scripts/gfm_demo.py arbitrary \
    --event-id Valencia_2024 \
    --bbox "-1.5 38.8 0.5 40.0" \
    --start-date 2024-10-29 \
    --end-date 2024-11-04

# KuroSiwo event — bbox and dates resolved from the catalogue
uv run python scripts/gfm_demo.py kurosiwo --ks-case KuroSiwo_470

# With harmonisation (re-encode to uint8 %, same grid)
uv run python scripts/gfm_demo.py arbitrary \
    --event-id Valencia_2024 \
    --bbox "-1.5 38.8 0.5 40.0" \
    --start-date 2024-10-29 \
    --end-date 2024-11-04 \
    --harmonise
```

The demo script mirrors `scripts/viirs_demo.py` in structure, but omits
VIIRS-specific flags (`--classify`, `--stream`, `--flood-threshold`) that
don't apply to GFM. A banner at runtime explains the 1-arcmin output.

## Quick start

```bash
uv run atlantis fetch \
  --event Valencia_2024 \
  --source gfm \
  --bbox "-1.5 38.8 0.5 40.0" \
  --start-date 2024-10-29 \
  --end-date 2024-11-04 \
  --harmonise \
  --no-keep-processed
```

This queries the EODC STAC API, processes Sentinel-1 tiles, and writes the
final harmonised 1-arcmin GeoTIFF (in `harmonised/`). `--harmonise` is needed
if you want that re-encoded uint8 output; without it, Atlantis writes only the
processed canonical-grid layers unless `--no-keep-processed` is also set:

```
harmonised/
  Valencia_2024_20241031_gfm_harmonised.tif    # uint8, 1 arcmin, flood % [0–100], nodata=255
plots/
  Valencia_2024_20241031_gfm_harmonised.png
```

Typical folder layout:

```
<output>/
  <event_id>/
    gfm/
      processed/
      plots/
      harmonised/
```

## Data source

| Property          | Value                                                                |
| ----------------- | -------------------------------------------------------------------- |
| Provider          | EODC (Earth Observation Data Centre)                                 |
| STAC API          | `https://stac.eodc.eu/api/v1`                                        |
| Collection        | `GFM`                                                                |
| Sensor            | Sentinel-1A / Sentinel-1B (C-band SAR)                               |
| Native resolution | ~20 m                                                                |
| Temporal cadence  | ~6-day revisit per sensor; joint coverage improves effective revisit |
| Atlantis uses     | `ensemble_flood_extent`, `reference_water_mask`                      |

## Upstream asset availability

EODC advertises additional GFM assets beyond the two Atlantis currently loads,
including:

- `ensemble_water_extent`
- `ensemble_likelihood`
- `exclusion_mask`
- `advisory_flags`
- `dlr_flood_extent`, `dlr_likelihood`
- `tuw_flood_extent`, `tuw_likelihood`
- `list_flood_extent`, `list_likelihood`

Atlantis does not yet provide a `--gfm-bands` selector or a raw-output mode
for those assets.

## Tips

- **Cloud-penetrating SAR** — GFM is available during cloud-covered events
  where VIIRS would be fully masked. Combine both products for maximum
  confidence.
- **Coarsen factor trade-off** — Larger `--gfm-coarsen-factor` (e.g. 8)
  speeds up processing and smooths speckle further; smaller values (e.g. 1)
  preserve native detail but increase runtime and sensitivity to noise.
- **Sentinel-1 revisit** — Sentinel-1A and 1B have ~6-day individual revisits.
  Widen `--start-date`/`--end-date` by a few days around the event peak to
  maximise the chance of capturing at least one acquisition.
- **No-keep-processed** — Use `--no-keep-processed` to skip writing the
  processed canonical-grid GeoTIFFs (`flood_fraction`, `quality_mask`,
  `permanent_water`) and save disk space. The harmonised flood output is still
  produced from in-memory data.
- **Multi-source overlay** — Because both GFM and VIIRS harmonised outputs
  snap to the same 1-arcmin global grid, array-based cross-product analysis
  requires no resampling.

## Further reading

- [Docs index](../README.md)
- [Python API](api.md)
- [Architecture and internals](internals.md)
- [Pipeline reference](pipeline.md)
- [VIIRS reference](../viirs/overview.md)
- [EODC STAC API](https://stac.eodc.eu/api/v1)
