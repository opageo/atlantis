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

| Asset                   | Meaning                                               |
| ----------------------- | ----------------------------------------------------- |
| `ensemble_flood_extent` | Flood classification: 0 = dry, 1 = flood, 255 = nodata |
| `reference_water_mask`  | Water type: 0 = land, 1 = water (seasonal/observed), 2 = permanent water, 255 = nodata |

Native product resolution is **~20 m** in the STAC COGs. Atlantis coarsens to
~80 m (default `--gfm-coarsen-factor 4`) before reprojection to reduce SAR
speckle and artefacts.

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

## Quick start

```bash
uv run atlantis fetch \
  --event Valencia_2024 \
  --source gfm \
  --bbox "-1.5 38.8 0.5 40.0" \
  --start-date 2024-10-29 \
  --end-date 2024-11-04 \
  --no-keep-processed --harmonise
```

This queries the EODC STAC API, processes Sentinel-1 tiles, and writes the
final harmonised 1-arcmin GeoTIFF (in `harmonised/`) alongside a PNG
visualisation:

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
| Bands used        | `ensemble_flood_extent`, `reference_water_mask`                      |

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
  intermediate ~80 m GeoTIFFs and save disk space. The harmonised output is
  still produced from in-memory data.
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
