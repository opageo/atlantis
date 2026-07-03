# Atlantis CLI Examples

A structured tour of the `atlantis fetch` CLI across its three flood-observation
sources — **VIIRS**, **MODIS**, and **GFM** — focused on the most important
pipeline options (`--classify/--no-classify`, `--stream/--no-stream`,
`--harmonise`, `--strategy`, and the source-specific backend/composite flags).

Throughout this guide, `--classify` means Atlantis emits **derived layers** and
`--no-classify` means it emits the **native layers** fetched from the source
unchanged. Run `pixi run atlantis list-layers` to inspect the full catalogue.

All commands use **pixi**. The same flags work for any AOI and date window; the
examples reuse the **Valencia 2024** flood (a Mediterranean DANA flash flood) as
a common AOI so you can compare sources and options side by side.

## Contents

- [Prerequisites](#prerequisites)
- [How to run](#how-to-run)
- [Common flags (all sources)](#common-flags-all-sources)
- [VIIRS examples](#viirs-examples)
- [MODIS examples](#modis-examples)
- [GFM examples](#gfm-examples)
- [Fetch all sources at once](#fetch-all-sources-at-once)
- [Predefined pixi tasks](#predefined-pixi-tasks)
- [Output structure](#output-structure)
- [More flood events](#more-flood-events)
- [VIIRS availability notes](#viirs-availability-notes)

## Prerequisites

```bash
pixi install        # create the env (geo stack + GDAL/HDF4)
pixi run setup      # restore data assets (VIIRS AOI grid, KuroSiwo catalogue)
```

- **VIIRS** streams public NOAA S3 tiles — no credentials needed.
- **GFM** reads Sentinel-1 COGs from the EODC STAC API — no credentials needed.
- **MODIS** needs a NASA Earthdata token in a `.env` file at the repo root
  (`EARTHDATA_TOKEN=...`, auto-loaded by the CLI). The `laads_hdf4` backend also
  requires a one-time LAADS license approval on your Earthdata account.

Quick smoke test (Valencia 2024, VIIRS):

```bash
pixi run demo
```

## How to run

There are two equivalent ways to run any example:

1. **Type the command** through pixi:

   ```bash
   pixi run atlantis fetch --event Valencia_2024 --source viirs \
     --bbox "-1.5 38.8 0.5 40.0" \
     --start-date 2024-10-29 --end-date 2024-11-04 \
     --harmonise --plot
   ```

   Add the top-level `--verbose` flag (before `fetch`) for debug logging:
   `pixi run atlantis --verbose fetch ...`.

2. **Run a predefined task** — see [Predefined pixi tasks](#predefined-pixi-tasks):

   ```bash
   pixi run demo          # VIIRS Valencia
   pixi run demo-modis    # MODIS Valencia
   pixi run demo-gfm      # GFM   Valencia
   ```

Throughout this guide the AOI/date flags are the Valencia 2024 window:

```
--bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04
```

Swap them for any other AOI/time window (see [More flood events](#more-flood-events)).

## Common flags (all sources)

| Flag                                                            | Default            | What it does                                                                                                       |
| --------------------------------------------------------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------ |
| `--source`, `-s`                                                | `all`              | `viirs`, `modis`, `gfm`, or `all`.                                                                                 |
| `--classify` / `--no-classify`                                  | `--classify`       | Emit Atlantis **derived layers**, or write the source-native layers unchanged.                                     |
| `--harmonise`                                                   | off                | Reproject the source-resolution `processed/` output to the canonical **1-arcmin** grid (stackable across sources). |
| `--stream` / `--no-stream`                                      | `--stream`         | Stream tiles via `/vsicurl/`, or download them first. (GFM always streams; MODIS streaming needs `lance_geotiff`.) |
| `--plot`                                                        | off                | Save a PNG per output date.                                                                                        |
| `--keep-processed` / `--no-keep-processed`                      | `--keep-processed` | Keep the intermediate source-resolution GeoTIFFs, or write only the harmonised output.                             |
| `--strategy`                                                    | `peak`             | `peak` (most-flooded date), `aggregate` (mean/mode composite), `all` (one output per date).                        |
| `--peak-window-days` / `--max-observations` / `--peak-priority` | `0` / `0` / `post` | Filter the date stack to a ±N-day window around the peak and subsample it.                                         |

> **Resolutions:** VIIRS **375 m**, MODIS **250 m**, GFM **~80 m** in `processed/`;
> all collapse to **1 arcmin** with `--harmonise`.

---

## VIIRS examples

Optical flood fraction from the NOAA VIIRS VFM product. Streams from public NOAA
S3 by default; no credentials required.

### 1. Quick start — peak date, classified + harmonised

```bash
pixi run atlantis fetch --event Valencia_2024 --source viirs \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --strategy peak --plot --harmonise
```

### 2. Stream vs. download (`--stream` / `--no-stream`)

```bash
# Stream tiles directly (default — nothing written to raw/)
pixi run atlantis fetch --event Valencia_2024 --source viirs \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --stream --harmonise

# Download tiles to raw/ first (re-runs, offline inspection)
pixi run atlantis fetch --event Valencia_2024 --source viirs \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --no-stream --harmonise
```

### 3. Derived vs. native layers (`--classify` / `--no-classify`)

```bash
# Derived layers (default): flood_fraction, quality_mask, permanent_water,
# plus VIIRS cloud_mask, snow_ice, and shadow
pixi run atlantis fetch --event Valencia_2024 --source viirs \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --classify --harmonise --plot

# Native VIIRS layer: raw VFM pixel codes, nearest-neighbour resampled when harmonised
pixi run atlantis fetch --event Valencia_2024 --source viirs \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --no-classify --harmonise --plot
```

### 4. Strategies (`peak` / `aggregate` / `all`)

```bash
# Temporal mean/mode composite over the window
pixi run atlantis fetch --event Valencia_2024 --source viirs \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --strategy aggregate --harmonise

# One harmonised GeoTIFF per date, peak-centred and subsampled
pixi run atlantis fetch --event Valencia_2024 --source viirs \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --strategy all --peak-window-days 2 --max-observations 3 --peak-priority balanced \
  --harmonise
```

### 5. Backend for the 2021–2022 gap (`--viirs-backend gmu_legacy`)

NOAA S3 covers 2012–2020 and 2023–2026. For events in the 2021–2022 gap, use the
GMU legacy backend (download-only, intermittent host):

```bash
pixi run atlantis fetch --event Pakistan_2022 --source viirs \
  --bbox "67.5 26 70 29.5" --start-date 2022-08-28 --end-date 2022-09-03 \
  --viirs-backend gmu_legacy --no-stream --harmonise --plot
```

---

## MODIS examples

MODIS MCDWD water/flood composites. Two backends: `lance_geotiff` (streamable,
~1-week NRT window) and `laads_hdf4` (download, full 2003+ archive). Requires
`EARTHDATA_TOKEN`.

### 1. Quick start — historical archive (`laads_hdf4`)

```bash
pixi run atlantis fetch --event Valencia_2024 --source modis \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --modis-backend laads_hdf4 --modis-composite F2 \
  --strategy peak --plot --harmonise
```

### 2. Backends & streaming (`--modis-backend`, `--stream`)

```bash
# NRT streaming — recent events only (~1-week retention); use a recent window
pixi run atlantis fetch --event Recent_Flood --source modis \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2026-06-24 --end-date 2026-06-28 \
  --modis-backend lance_geotiff --stream --harmonise

# Historical archive — downloads HDF4 (`--stream` is ignored)
pixi run atlantis fetch --event Valencia_2024 --source modis \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --modis-backend laads_hdf4 --harmonise
```

### 3. Composite selection (`--modis-composite`)

```bash
# F1 (1-day), F1C (1-day cloud-shadow-screened), F2 (2-day, default), F3 (3-day)
pixi run atlantis fetch --event Valencia_2024 --source modis \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --modis-backend laads_hdf4 --modis-composite F3 --harmonise
```

### 4. Derived vs. native layers (`--classify` / `--no-classify`)

With `--classify`, MODIS writes the derived `flood_fraction`, `quality_mask`,
`permanent_water`, and `recurring_flood` layers. With `--no-classify`, it writes
the native `raw` MCDWD composite unchanged.

```bash
# Raw MCDWD codes: 0=no-water, 1=water, 2=recurring, 3=unusual flood, 255=insufficient
pixi run atlantis fetch --event Valencia_2024 --source modis \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --modis-backend laads_hdf4 --no-classify --harmonise --plot
```

### 5. Time-series (`--strategy all`)

```bash
pixi run atlantis fetch --event Valencia_2024 --source modis \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --modis-backend laads_hdf4 --strategy all \
  --peak-window-days 2 --max-observations 3 --harmonise
```

---

## GFM examples

Sentinel-1 SAR flood extent from the EODC Global Flood Monitor. Cloud-penetrating
and **always streamed** via STAC/COG (no credentials; `--no-stream` is ignored).

### 1. Quick start — classified + harmonised

```bash
pixi run atlantis fetch --event Valencia_2024 --source gfm \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --strategy peak --plot --harmonise
```

### 2. Derived vs. native SAR layers (`--classify` / `--no-classify`)

```bash
# Derived layers (default): flood_fraction, quality_mask, permanent_water
pixi run atlantis fetch --event Valencia_2024 --source gfm \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --classify --harmonise --plot

# Native bands: ensemble_flood_extent + reference_water_mask (no derivation)
pixi run atlantis fetch --event Valencia_2024 --source gfm \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --no-classify --harmonise --plot
```

### 3. Resolution / speckle trade-off (`--gfm-coarsen-factor`)

```bash
# Keep more detail (~40 m): factor 2 — slower, noisier
pixi run atlantis fetch --event Valencia_2024 --source gfm \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --gfm-coarsen-factor 2 --harmonise

# Smoother / faster (~160 m): factor 8
pixi run atlantis fetch --event Valencia_2024 --source gfm \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --gfm-coarsen-factor 8 --harmonise
```

### 4. Time-series (`--strategy all`)

```bash
pixi run atlantis fetch --event Valencia_2024 --source gfm \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --strategy all --peak-window-days 2 --max-observations 3 --harmonise
```

---

## Fetch all sources at once

`--source all` runs VIIRS, MODIS, and GFM in one call (MODIS still needs its
backend + token). Each source writes to its own subfolder under `--output`.

```bash
pixi run atlantis fetch --event Valencia_2024 --source all \
  --bbox "-1.5 38.8 0.5 40.0" --start-date 2024-10-29 --end-date 2024-11-04 \
  --modis-backend laads_hdf4 --strategy peak --harmonise --plot \
  --output ./data/Valencia_2024
```

## Predefined pixi tasks

Ready-to-run tasks defined in [`pixi.toml`](pixi.toml):

| Task                      | Equivalent to                                       |
| ------------------------- | --------------------------------------------------- |
| `pixi run demo`           | VIIRS, Valencia 2024 (peak-window, plot, harmonise) |
| `pixi run demo-modis`     | MODIS (`laads_hdf4`), Valencia 2024                 |
| `pixi run demo-gfm`       | GFM, Valencia 2024                                  |
| `pixi run examples-viirs` | VIIRS across Harvey / Bihar / Vamco / West Africa   |
| `pixi run examples-modis` | MODIS across the same four events                   |
| `pixi run examples-gfm`   | GFM across five events (incl. Valencia)             |
| `pixi run examples`       | Everything above                                    |

## Output structure

A classified run with `--harmonise --plot` writes, per source:

```
<output>/<event_id>/<source>/
  processed/    # source-resolution GeoTIFFs (omitted with --no-keep-processed)
  plots/        # one PNG per date (with --plot), incl. *_harmonised.png
  harmonised/   # 1-arcmin GeoTIFFs (with --harmonise)
```

With `--classify`, `processed/` and `harmonised/` hold derived layers. With
`--no-classify`, they hold the native source layers instead — e.g. GFM
`*_ensemble_flood_extent.tif` / `*_reference_water_mask.tif`, or VIIRS/MODIS
`*_raw.tif`.

## More flood events

The AOI/date flags are the only thing that changes between events — swap them
into any example above. Some KuroSiwo-derived AOIs:

| Event                               | bbox `"W S E N"`            | Date window             |
| ----------------------------------- | --------------------------- | ----------------------- |
| Valencia 2024 (Spain)               | `-1.5 38.8 0.5 40.0`        | 2024-10-29 → 2024-11-04 |
| Hurricane Harvey 2017 (USA)         | `-97.27 28.24 -95.54 29.80` | 2017-08-28 → 2017-08-31 |
| Bihar / Nepal monsoon 2019          | `84.84 24.92 86.49 26.16`   | 2019-09-16 → 2019-09-20 |
| Typhoon Vamco 2020 (Philippines)    | `121.14 16.72 122.25 18.45` | 2020-11-12 → 2020-11-14 |
| West Africa 2020 (Ghana/Togo/Benin) | `-0.86 8.26 1.99 11.73`     | 2020-10-13 → 2020-10-15 |

For KuroSiwo cases you can auto-resolve bbox + dates with the VIIRS helper:

```bash
pixi run atlantis fetch-kurosiwo-viirs --case KuroSiwo_470 --harmonise --plot
```

## VIIRS availability notes

The default `noaa_s3` backend publishes VFM tiles for **2012–2020 and 2023–2026**.
**2021 and 2022 are not published** on the public NOAA bucket. For events in that
gap (e.g. Pakistan 2022), use `--viirs-backend gmu_legacy --no-stream` (the GMU
host is intermittently offline — retry from a non-cloud network).

See [`docs/cli.md`](docs/cli.md) for the full flag reference and
[`docs/viirs/overview.md#data-availability`](docs/viirs/overview.md#data-availability)
for the backend comparison.
