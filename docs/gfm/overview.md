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
| `reference_water_mask`  | Water type: 0 = no water, 1 = permanent water, 2 = seasonal water, 255 = nodata        |

Native product resolution is **~20 m** in the STAC COGs. Atlantis coarsens to
~80 m (default `--gfm-coarsen-factor 4`) before reprojection to reduce SAR
speckle and artefacts.

Data is accessed via the **EODC STAC API** (`https://stac.eodc.eu/api/v1`,
collection `GFM`) using Cloud-Optimised GeoTIFFs — no separate download step
is required.

### Native vs derived layers

Atlantis exposes GFM as **native** and **derived** layers, but the exact
inventory is maintained only in the canonical
[GFM layer reference](../layers.md#layers-gfm) and via
`atlantis list-layers --source gfm`.

- **Native layers** are fetched untouched with `--no-classify`.
- **Derived layers** are computed from observation counts with `--classify`.

`flood_fraction` is therefore a **derived** layer — and, unlike VIIRS/MODIS, it is built from _observation counts_, not raw codes. The EODC COG encoding of `reference_water_mask` **follows GFM PDD Table 20**: `0 = no water`, `1 = permanent water`, `2 = seasonal water`, `255 = nodata`. This was confirmed against fetched source COGs via a month-stability test (code `1` is byte-identical across the monthly masks → permanent; code `2` varies by month → seasonal). An earlier assumption that the permanent/seasonal codes were swapped was a bug, now corrected in `atlantis.fetchers.gfm.layers`; the seasonal class (`2`) is the GFM analog of MODIS `recurring_flood`.

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

| Aspect                         | VIIRS / MODIS                                                                                                    | GFM                                                                                                                                                                                                                                                                                                           |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Fetched resolution**         | Native (375 m / 250 m, EPSG:4326)                                                                                | Native ~20 m (Sentinel-1), processed to **~80 m EPSG:4326** (coarsen ×4)                                                                                                                                                                                                                                      |
| **`--harmonise` effect**       | Reprojects to 1-arcmin + encodes uint8 %                                                                         | Same pattern: reprojects the ~80 m processed output to 1-arcmin. Classified: `average`-resample `flood_fraction` (uint8 %). Native: NN-resample uint8 codes. **Off by default in the CLI (use `--harmonise`).**                                                                                               |
| **Raw mode (`--no-classify`)** | Available — writes raw integer pixel codes                                                                       | Available — writes `ensemble_flood_extent` (0=dry,1=flood,255=nodata) and `reference_water_mask` (0=land,1=water,2=perm,255=nodata) on the ~80 m processed grid (nearest-neighbour); `--harmonise` downsamples to 1-arcmin.                                                                                   |
| **Stream / download toggle**   | `--stream` / `--no-stream`                                                                                       | Always streamed via `odc.stac` (`--no-stream` is ignored with a warning)                                                                                                                                                                                                                                      |
| **Setup requirement**          | VIIRS needs `uv run python scripts/setup.py` for the AOI grid                                                    | None — data accessed directly via the EODC STAC API                                                                                                                                                                                                                                                           |
| **Python default `strategy`**  | `"peak"`                                                                                                         | `"peak"`                                                                                                                                                                                                                                                                                                      |
| **Dataset variables**          | Classified fractions plus shared masks (`water_fraction`, `flood_fraction`, `reference_water`, `exclusion_mask`) | Classified: `water_fraction` (float32), `flood_fraction` (float32), `reference_water` (uint8), plus native-code companions such as `exclusion_mask`. Native: `ensemble_flood_extent`, `ensemble_water_extent`, `reference_water_mask`, `exclusion_mask`, `ensemble_likelihood`, `advisory_flags` (all uint8). |
| **Harmonised output**          | uint8 [0–100], nodata=255, canonical 1-arcmin grid                                                               | Classified: same format — stackable with VIIRS / MODIS without resampling. Native: uint8 codes, 1-arcmin, nodata=255.                                                                                                                                                                                         |

Once `--harmonise` resamples GFM `processed/` output to the canonical 1-arcmin
grid, GFM, VIIRS, and MODIS harmonised outputs over the same AOI are **directly
stackable** without any further resampling.

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

# With harmonisation (resample ~80 m → canonical 1-arcmin)
uv run python scripts/gfm_demo.py arbitrary \
    --event-id Valencia_2024 \
    --bbox "-1.5 38.8 0.5 40.0" \
    --start-date 2024-10-29 \
    --end-date 2024-11-04 \
    --harmonise
```

The demo script mirrors `scripts/viirs_demo.py` in structure, but omits
VIIRS-specific flags (`--stream`, `--flood-threshold`) that don't apply to GFM.
A banner at runtime explains the 1-arcmin output.

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

This queries the EODC STAC API, processes Sentinel-1 tiles, and writes a
1-arcmin harmonised GeoTIFF (re-encodes float32 → uint8 %). Add `--plot` to
also save a PNG. Use `--no-classify` instead to emit the native SAR band codes
(`ensemble_flood_extent`, `reference_water_mask`) without any derivation:

```bash
# Native / raw mode — emit discrete codes, no flood_fraction derivation
uv run atlantis fetch \
  --event Valencia_2024 --source gfm \
  --bbox "-1.5 38.8 0.5 40.0" \
  --start-date 2024-10-29 --end-date 2024-11-04 \
  --no-classify --no-keep-processed
```

Typical folder layout (classified mode with `--harmonise`):

```
<output>/
  <event_id>/
    gfm/
      processed/
      plots/
      harmonised/
        <event_id>_<date_token>_gfm_harmonised.tif    # uint8, 1 arcmin, flood % [0–100]
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
