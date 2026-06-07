# Atlantis CLI Examples

A tour of `atlantis` CLI workflows across real flood events. Each example is
self-contained and can be copy-pasted after `make setup`.

Every case is shown in **two equivalent forms**:

1. **Generic CLI** (`atlantis fetch`) — the user supplies a bounding box and
   a date range. This is the default workflow and works for any flood event,
   anywhere. No prior knowledge of KuroSiwo (or any catalogue) is required.
2. **KuroSiwo helper** (`atlantis fetch-kurosiwo-viirs`) — convenience
   wrapper that auto-resolves bbox + dates from the bundled KuroSiwo
   catalogue when you already know the case ID.

The bounding boxes and dates used in the generic examples below happen to be
derived from
[`data/metadata/kurosiwo_metadata_v1.csv`](data/metadata/kurosiwo_metadata_v1.csv)
so the two forms produce the same outputs — but the generic form would work
identically for any AOI/time window you choose. For background on the
KuroSiwo dataset itself, see
[`notebooks/drafts/kurosiwo_eda.ipynb`](notebooks/drafts/kurosiwo_eda.ipynb).

## Contents

- [Prerequisites](#prerequisites)
- [Common flags](#common-flags)
- [Case 1 — Valencia, Spain (October 2024)](#case-1--valencia-spain-october-2024)
- [Case 2 — Hurricane Harvey, Texas, USA (August 2017)](#case-2--hurricane-harvey-texas-usa-august-2017)
- [Case 3 — South Asian Monsoon, Bihar / Nepal (September 2019)](#case-3--south-asian-monsoon-bihar--nepal-september-2019)
- [Case 4 — Typhoon Vamco, Luzon, Philippines (November 2020)](#case-4--typhoon-vamco-luzon-philippines-november-2020)
- [Case 5 — West Africa floods, Ghana / Togo / Benin (October 2020)](#case-5--west-africa-floods-ghana--togo--benin-october-2020)
- [Batch KuroSiwo runs](#batch-kurosiwo-runs)
- [VIIRS availability notes](#viirs-availability-notes)

## Prerequisites

```bash
make setup   # install deps + restore data assets (VIIRS AOI grid, KuroSiwo catalogue)
```

Or, equivalently:

```bash
uv sync --extra geo
uv run atlantis setup
```

A quick smoke test (Valencia 2024 with sensible defaults):

```bash
uv run atlantis demo   # equivalent to: make demo
```

Each case below also has a one-liner `make` target. Run them all with
`make examples` (KuroSiwo helper) or `make examples-bbox` (generic CLI),
or individually:

| Case                                  | Generic CLI (bbox + date)      | KuroSiwo helper                 |
| ------------------------------------- | ------------------------------ | ------------------------------- |
| Valencia 2024 (Spain)                 | `make demo`                    | — (Valencia is not in KuroSiwo) |
| Hurricane Harvey (Texas, USA)         | `make example-harvey-bbox`     | `make example-harvey`           |
| South Asian monsoon (Bihar/Nepal)     | `make example-bihar-bbox`      | `make example-bihar`            |
| Typhoon Vamco (Luzon, Philippines)    | `make example-vamco-bbox`      | `make example-vamco`            |
| West Africa floods (Ghana/Togo/Benin) | `make example-westafrica-bbox` | `make example-westafrica`       |

## Common flags

These appear throughout the examples below — abridged from
[`docs/viirs.md`](docs/viirs.md). For pixel-level details on what each
`--strategy` actually does to the multi-date stack (mean vs. mode vs.
per-date pass-through), see
[`docs/viirs_pipeline.md`](docs/viirs_pipeline.md#strategies-in-detail-pixel-level).

- **`--strategy peak`** (default) — keep the date with the most flood pixels.
- **`--strategy aggregate`** — temporal mean (continuous) / mode (categorical) composite over the window.
- **`--strategy all`** — write one harmonised output per date (time-series).
- **`--no-keep-processed`** — skip intermediate 375 m files; write only the harmonised output.
- **`--harmonise`** — resample to 1 arcmin (~1.85 km) on a global grid.
- **`--plot`** — save a PNG of the peak-flood date.
- **`--stream` / `--no-stream`** — stream tiles from NOAA S3 (default) or download to `raw/`.
- **`--no-classify`** — write raw integer pixel codes (single GeoTIFF) instead of flood/quality/permanent-water masks.
- **`--verbose`** — enable debug-level logging for fetch/harmonise internals.

## Case 1 — Valencia, Spain (October 2024)

Mediterranean DANA flash flood. Used as the default smoke test (`atlantis demo`).

```bash
uv run atlantis --verbose fetch \
  --event Valencia_2024 \
  --source viirs \
  --bbox "-1.5 38.8 0.5 40.0" \
  --start-date 2024-10-29 \
  --end-date 2024-11-04 \
  --plot \
  --harmonise \
  --output ./data/Valencia_2024
```

**Example output:**

```
Fetching data for event: Valencia_2024
Sources: viirs
Output: data/Valencia_2024

Fetching from viirs...
  2024-10-29  flood_fraction  flooded=12 847  valid=38 400  fraction=0.334
  2024-10-30  flood_fraction  flooded= 9 211  valid=38 400  fraction=0.240
  2024-10-31  flood_fraction  flooded= 5 034  valid=38 400  fraction=0.131
  2024-11-01  flood_fraction  flooded= 1 872  valid=38 400  fraction=0.049
  Wrote 4 files
  Harmonised → Valencia_2024_2024-10-29_viirs_harmonised.tif
```

Generates:

- `viirs/processed/` — 375 m classified GeoTIFFs for all dates
- `viirs/plots/Valencia_2024_2024-10-29_viirs.png` — peak-date visualisation (375 m)
- `viirs/plots/Valencia_2024_2024-10-29_viirs_harmonised.png` — harmonised visualisation (1 arcmin)
- `viirs/harmonised/Valencia_2024_2024-10-29_viirs_harmonised.tif` — 1 arcmin GeoTIFF

## Case 2 — Hurricane Harvey, Texas, USA (August 2017)

One of the largest KuroSiwo events: ~1,227 km² of flood extent across the
Texas Gulf coast (Houston metropolitan area). A good stress test for
multi-tile mosaicing.

**Generic CLI** — bbox + date range, no catalogue needed (`make example-harvey-bbox`):

```bash
uv run atlantis --verbose fetch \
  --event Harvey_2017 \
  --source viirs \
  --bbox "-97.27 28.24 -95.54 29.80" \
  --start-date 2017-08-28 --end-date 2017-08-31 \
  --plot --harmonise --no-keep-processed \
  --output ./data/Harvey_2017
```

**KuroSiwo helper** — same event, bbox/dates auto-resolved from the
catalogue (`make example-harvey`):

```bash
uv run atlantis --verbose fetch-kurosiwo-viirs \
  --case KuroSiwo_1111004 \
  --days-before 1 --days-after 1 \
  --plot --harmonise --no-keep-processed \
  --output ./data/KuroSiwo_1111004
```

## Case 3 — South Asian Monsoon, Bihar / Nepal (September 2019)

Ganges-basin monsoon flooding: ~1,115 km² of flood extent across
northern India and southern Nepal. Demonstrates the `aggregate` strategy
over a slightly wider window — useful when daily acquisitions are
cloud-contaminated and a temporal composite is more informative.

**Generic CLI** — bbox + date range with `--strategy aggregate`
(`make example-bihar-bbox`):

```bash
uv run atlantis --verbose fetch \
  --event Bihar_2019 \
  --source viirs \
  --bbox "84.84 24.92 86.49 26.16" \
  --start-date 2019-09-16 --end-date 2019-09-20 \
  --strategy aggregate \
  --plot --harmonise --no-keep-processed \
  --output ./data/Bihar_2019
```

**KuroSiwo helper** (`make example-bihar`):

```bash
uv run atlantis --verbose fetch-kurosiwo-viirs \
  --case KuroSiwo_1111007 \
  --days-before 2 --days-after 2 \
  --plot --harmonise --no-keep-processed \
  --output ./data/KuroSiwo_1111007
```

## Case 4 — Typhoon Vamco, Luzon, Philippines (November 2020)

Tropical-cyclone driven flooding north of Manila: ~951 km² extent.

**Generic CLI** — bbox + date range (`make example-vamco-bbox`):

```bash
uv run atlantis --verbose fetch \
  --event Vamco_2020 \
  --source viirs \
  --bbox "121.14 16.72 122.25 18.45" \
  --start-date 2020-11-12 --end-date 2020-11-14 \
  --plot --harmonise --no-keep-processed \
  --output ./data/Vamco_2020
```

**KuroSiwo helper** (`make example-vamco`):

```bash
uv run atlantis --verbose fetch-kurosiwo-viirs \
  --case KuroSiwo_1111011 \
  --days-before 1 --days-after 1 \
  --plot --harmonise --no-keep-processed \
  --output ./data/KuroSiwo_1111011
```

**Variant** — build a daily time-series with `--strategy all` over a wider window:

```bash
uv run atlantis --verbose fetch \
  --event Vamco_2020_timeseries \
  --source viirs \
  --bbox "121.14 16.72 122.25 18.45" \
  --start-date 2020-11-11 --end-date 2020-11-15 \
  --strategy all \
  --harmonise --no-keep-processed \
  --output ./data/Vamco_2020_timeseries
```

This writes one harmonised GeoTIFF per date in `viirs/harmonised/` and a
matching PNG per date in `viirs/plots/`, e.g.
`Vamco_2020_timeseries_2020-11-13_viirs_harmonised.tif`.

**Variant** — wide search window with a peak-centred filter and subsampling:

```bash
uv run atlantis --verbose fetch \
  --event Vamco_2020_window \
  --source viirs \
  --bbox "121.14 16.72 122.25 18.45" \
  --start-date 2020-11-07 --end-date 2020-11-19 \
  --strategy all \
  --peak-window-days 4 \
  --max-observations 5 \
  --peak-priority post \
  --harmonise --no-keep-processed \
  --output ./data/Vamco_2020_window
```

This searches 13 days, detects the peak flood date, filters to ±4 days around it,
then returns up to 5 dates (peak + 4 nearest post-event days). See
[`docs/viirs.md#peak-window-filtering-and-subsampling`](docs/viirs.md#peak-window-filtering-and-subsampling)
for the full flag reference.

## Case 5 — West Africa floods, Ghana / Togo / Benin (October 2020)

Tropical/sub-Saharan flooding: ~420 km² extent. Used in
[`docs/viirs.md`](docs/viirs.md) as the canonical KuroSiwo example.

**Generic CLI** — bbox + date range (`make example-westafrica-bbox`):

```bash
uv run atlantis --verbose fetch \
  --event WestAfrica_2020 \
  --source viirs \
  --bbox "-0.86 8.26 1.99 11.73" \
  --start-date 2020-10-13 --end-date 2020-10-15 \
  --plot --harmonise --no-keep-processed \
  --output ./data/WestAfrica_2020
```

**KuroSiwo helper** (`make example-westafrica`):

```bash
uv run atlantis --verbose fetch-kurosiwo-viirs \
  --case KuroSiwo_470 \
  --plot --harmonise --no-keep-processed \
  --output ./data/KuroSiwo_470
```

## Batch KuroSiwo runs

Process the first N catalogue cases in one go (skipping intermediates to
keep disk usage low):

```bash
uv run atlantis --verbose fetch-kurosiwo-viirs \
  --catalogue assets/ks_catalogue.gpkg \
  --limit 5 \
  --no-keep-processed --harmonise \
  --output ./data/kurosiwo_batch
```

For faster repeated runs, pre-build the metadata CSV once and reuse it:

```bash
uv run atlantis --verbose build-kurosiwo-metadata \
  --catalogue assets/ks_catalogue.gpkg \
  --output data/metadata/kurosiwo_metadata_v1.csv

uv run atlantis --verbose fetch-kurosiwo-viirs \
  --metadata data/metadata/kurosiwo_metadata_v1.csv \
  --case KuroSiwo_1111011 \
  --harmonise --no-keep-processed
```

## VIIRS availability notes

The default `noaa_s3` backend publishes VFM tiles for **2012–2020 and
2023–2026** (verified at time of writing). **2021 and 2022 are not
published** on the public NOAA JPSS bucket. Some notable KuroSiwo events
fall in this gap, e.g.:

- `KuroSiwo_1111009` — Pakistan, August–September 2022 (~9,300 km², the largest event in the catalogue)
- `KuroSiwo_554`/`555`/`559`/`561`/`562`/`567` — various 2021–2022 events

For those, use the GMU Legacy backend with `--viirs-backend gmu_legacy --no-stream`.
The GMU host is intermittently offline — retry from a non-cloud network if
connections time out.

**Pakistan 2022 — GMU Legacy backend example:**

```bash
uv run atlantis --verbose fetch \
  --event Pakistan_2022 \
  --source viirs \
  --bbox "67.5 26 70 29.5" \
  --start-date 2022-08-28 --end-date 2022-09-03 \
  --viirs-backend gmu_legacy --no-stream \
  --plot --harmonise --no-keep-processed \
  --output ./data/Pakistan_2022
```

See [`docs/viirs.md#data-availability`](docs/viirs.md#data-availability) for
the full backend comparison.
