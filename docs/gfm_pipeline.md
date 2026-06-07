# GFM Flood Detection

**SAR-based flood mapping from Sentinel-1**

Atlantis integrates the **Global Flood Monitor (GFM)** — a near-real-time flood
extent product derived from Sentinel-1 SAR imagery, operated by the EODC
(Earth Observation Data Centre). Unlike optical products (e.g. VIIRS), SAR
penetrates cloud cover, making GFM reliable during the heavy rainfall that
typically accompanies flood events.

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

## CLI reference

### `atlantis fetch --source gfm`

```bash
uv run atlantis fetch \
  --event <event_id> \
  --source gfm \
  --bbox "<west> <south> <east> <north>" \
  --start-date YYYY-MM-DD \
  --end-date YYYY-MM-DD \
  [flags]
```

### `atlantis harmonise --source gfm`

Resample previously fetched GFM outputs to a uniform 1-arcmin grid:

```bash
uv run atlantis harmonise \
  --event Valencia_2024 \
  --source gfm
```

## Flags

### Output control

| Flag                  | Default     | Effect                                                                                               |
| --------------------- | ----------- | ---------------------------------------------------------------------------------------------------- |
| `--harmonise`         | off         | Produce a resampled 1-arcmin flood-fraction GeoTIFF                                                 |
| `--no-keep-processed` | off         | Write only the harmonised output (no intermediate ~80 m files)                                      |
| `--plot`              | off         | Save a PNG of each result date                                                                       |
| `--strategy`          | `peak`      | Multi-date reduction: `peak` (most-flooded date), `aggregate` (mean/mode composite), `all` (per-date outputs) |

### Processing

| Flag                   | Default   | Effect                                                       |
| ---------------------- | --------- | ------------------------------------------------------------ |
| `--gfm-coarsen-factor` | `4`       | Spatial coarsening factor before reprojection. Reduces native ~20 m to ~80 m by default. Higher values trade resolution for speed/noise reduction. |
| `--gfm-resampling`     | `average` | Resampling method when reprojecting to EPSG:4326. Any rasterio method name is accepted. |

### Harmonisation

| Flag                  | Default | Effect                                      |
| --------------------- | ------- | ------------------------------------------- |
| `--target-resolution` | 0.0167° | Target grid spacing (1 arcmin default)      |
| `--dry-run`           |         | Show what would be processed without acting |

## Pipeline in detail

The GFM processing pipeline operates per-date and per-item. Each STAC item
corresponds to a single Sentinel-1 acquisition over one Sentinel-2 tile
footprint. Multiple items can cover the same date and bbox.

### Step-by-step

```
STAC search → group by date → per-item loop → classify → accumulate → harmonise
                                    │
                    load (native CRS, ~20 m, odc.stac)
                    coarsen (max-pool × coarsen_factor)
                    compute binary masks (flood, perm, valid)
                    reproject to canonical 1-arcmin EPSG:4326
                    │
                (flood_count, perm_water_count, valid_count) ← accumulate
                    │
                    classify:
                        flood_fraction  = flood_count / valid_count    [0, 1], NaN where unobserved
                        quality_mask    = (valid_count > 0).uint8      {0, 1}
                        permanent_water = (perm_ratio > 0.5).uint8     {0, 1}
```

#### Why binary masks before reprojection?

`ensemble_flood_extent` has discrete codes (0 = dry, 1 = flood, 255 = nodata).
Applying `Resampling.average` directly on these codes would produce fractional
intermediates like 0.5 — which cannot be reliably thresholded back to 0 or 1.
Instead, Atlantis converts to a float32 binary mask *first* (at the coarsened
native resolution where codes are still discrete), then reprojects with
`average` resampling. After reprojection each pixel contains the *fraction of
its area* that was flooded — exactly what we want to accumulate across items.

#### Why max-pool for coarsening?

The max-pool preserves the flood signal: if any sub-pixel in the coarsened
neighbourhood is flooded (code 1), the coarsened result is 1. Alternatives
like mean would dilute the signal and risk rounding flood pixels to 0 before
the binary mask step.

#### Reprojection to the canonical grid

After computing the per-item binary masks in the native UTM CRS, Atlantis
reprojects each mask directly onto the **canonical 1-arcmin global EPSG:4326
grid** — the same grid used by ECMWF's `Globe_flood_area_*.grb` and VIIRS
harmonised outputs. This means:

- The bbox is snapped outward to the nearest cell edge of the global grid.
- Every output pixel centre satisfies `(lon + 180) × 60 − 0.5 ∈ ℤ` and
  `(90 − lat) × 60 − 0.5 ∈ ℤ`.
- GFM and VIIRS harmonised outputs over the same AOI are **stackable** without
  any further resampling.

See [Canonical 1-arcmin global grid](viirs.md#canonical-1-arcmin-global-grid)
for the full alignment specification.

## Strategies in detail

### `peak` — single most-flooded date

Implemented in [`atlantis.fetchers.gfm.selection.flood_pixel_count`](../src/atlantis/fetchers/gfm/selection.py).

For each date `d`, count the flooded pixels:

$$
\text{flood\_count}_d = \sum_{(i,j)} \mathbb{1}\!\left[\text{flood\_fraction}_d(i,j) > 0\right]
$$

(NaN pixels — where no valid observation exists — are excluded from the count.)

Pick:

$$
d^{\star} = \arg\max_d \text{flood\_count}_d
$$

Ties go to the **earliest** date (first to reach the max during iteration).
The output filename carries only the single winning date token, e.g.
`Valencia_2024_20241031_gfm_harmonised.tif`.

### `aggregate` — temporal composite

All dates are stacked and reduced element-wise:

| Layer             | Reduction                                         | Rationale                               |
| :---------------- | :------------------------------------------------ | :-------------------------------------- |
| `flood_fraction`  | `np.nanmean(stack, axis=0)`                       | Continuous variable → arithmetic mean   |
| `quality_mask`    | `np.any(stack > 0, axis=0)`                       | 1 if any date had valid data            |
| `permanent_water` | majority vote (`mean(stack, axis=0) > 0.5`)       | Most-frequent value across dates        |
| `cloud_fraction`  | scalar `1 − valid_pixels/total_pixels`            | Tile-level metadata                     |

`nanmean` means pixels that were unobserved (NaN) on some dates are averaged
over the dates that *did* observe them — no bias toward missing data.

The output `date_token` spans the full range:
`{first_date}_{last_date}`, e.g. `20241030_20241101`. For a single date the
token is just `20241030`.

### `all` — every date independently

No reduction. Each date's processed tile becomes a separate `FetchResult` with
its own date token. When `--harmonise` is set, each date produces its own
harmonised GeoTIFF + PNG.

## Output structure

```
<output>/
  <event_id>/
    gfm/
      processed/    # absent with --no-keep-processed
        <event_id>_<YYYYMMDD>_gfm_flood_fraction.tif    # float32, nodata=-9999
        <event_id>_<YYYYMMDD>_gfm_quality_mask.tif      # uint8, nodata=255
        <event_id>_<YYYYMMDD>_gfm_permanent_water.tif   # uint8, nodata=255
      plots/        # with --plot
        <event_id>_<date_token>_gfm.png
        <event_id>_<date_token>_gfm_harmonised.png      # with --harmonise
      harmonised/   # with --harmonise
        <event_id>_<date_token>_gfm_harmonised.tif
```

## Output format

### Processed outputs (~80 m, native UTM → EPSG:4326)

| File                  | Dtype   | Nodata  | Values                          |
| --------------------- | ------- | ------- | ------------------------------- |
| `*_flood_fraction.tif` | float32 | -9999.0 | [0, 1] — fraction of obs flooded; NaN → nodata |
| `*_quality_mask.tif`   | uint8   | 255     | 1 = valid observation, 0 = no data |
| `*_permanent_water.tif`| uint8   | 255     | 1 = permanent water, 0 = not    |

- **CRS**: EPSG:4326 (WGS84)
- **Compression**: LZW
- Resolution varies with native GSD and `--gfm-coarsen-factor`

### Harmonised output (1 arcmin)

| Property    | Value                                                     |
| ----------- | --------------------------------------------------------- |
| **CRS**     | EPSG:4326 (WGS84)                                         |
| **Dtype**   | uint8                                                     |
| **Nodata**  | 255                                                       |
| **Values**  | 0–100 (flood fraction as integer percentage)              |
| **Resolution** | 1/60° ≈ 1.85 km at the equator                        |
| **Grid**    | Canonical global grid, pixel centres at `±(k+0.5)/60°`   |
| **Compression** | LZW                                                   |

Harmonised flood extent values are stored as **integer percentages** (0–100),
where 0 = no flood and 100 = fully flooded (same encoding as VIIRS harmonised
outputs). This gives 1% precision while using 4× less disk space than float32.

Compatible with `rioxarray`, `rasterio`, QGIS, and any GDAL-based tool.

## Data source

| Property          | Value                                                                  |
| ----------------- | ---------------------------------------------------------------------- |
| Provider          | EODC (Earth Observation Data Centre)                                   |
| STAC API          | `https://stac.eodc.eu/api/v1`                                          |
| Collection        | `GFM`                                                                  |
| Sensor            | Sentinel-1A / Sentinel-1B (C-band SAR)                                 |
| Native resolution | ~20 m                                                                  |
| Temporal cadence  | ~6-day revisit per sensor; joint coverage improves effective revisit   |
| Bands used        | `ensemble_flood_extent`, `reference_water_mask`                        |

Override the API endpoint via `ATLANTIS_GFM_API_URL` or programmatically
through `FetcherConfig`.

## Configuration reference

| Config field          | Env var                     | Default   | Meaning                                               |
| --------------------- | --------------------------- | --------- | ----------------------------------------------------- |
| `gfm_api_url`         | `ATLANTIS_GFM_API_URL`      | EODC URL  | STAC API endpoint                                     |
| `gfm_coarsen_factor`  | `ATLANTIS_GFM_COARSEN_FACTOR` | `4`     | Spatial coarsening factor applied before reprojection |
| `gfm_resampling`      | `ATLANTIS_GFM_RESAMPLING`   | `average` | Resampling method for reprojection to EPSG:4326       |
| `target_resolution`   | `ATLANTIS_TARGET_RESOLUTION` | `1/60`   | Harmonised output resolution in degrees               |
| `snap_to_global_grid` | `ATLANTIS_SNAP_TO_GLOBAL_GRID` | `True` | Align harmonised output to canonical global grid      |

All config fields can also be set in a `.env` file at the repository root.

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

- [Canonical 1-arcmin global grid](viirs.md#canonical-1-arcmin-global-grid) — alignment details shared with VIIRS
- [Pipeline vision](../src/README.md)
- [EODC STAC API](https://stac.eodc.eu/api/v1)
- End-to-end tests: `tests/fetchers/test_gfm_e2e.py`
