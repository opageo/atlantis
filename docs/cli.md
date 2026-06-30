# Atlantis CLI Reference

Full reference for the `atlantis` command-line interface. For task-oriented
walkthroughs (Valencia, Harvey, Bihar, …) see
[../CLI_Examples.md](../CLI_Examples.md). For per-source pipeline details
see [viirs/overview.md](viirs/overview.md), [modis/overview.md](modis/overview.md),
and [gfm/overview.md](gfm/overview.md).

All commands are invoked through `uv run atlantis <command>` (or simply
`atlantis <command>` inside an activated environment).

## Contents

- [Global options](#global-options)
- [Command summary](#command-summary)
- [`setup`](#setup)
- [`demo`](#demo)
- [`fetch`](#fetch)
- [`fetch-kurosiwo-viirs`](#fetch-kurosiwo-viirs)
- [`fetch-kurosiwo-modis`](#fetch-kurosiwo-modis)
- [`build-kurosiwo-metadata`](#build-kurosiwo-metadata)
- [`harmonise`](#harmonise)
- [`archive`](#archive) _(placeholder)_
- [`validate`](#validate) _(placeholder)_
- [`list-sources`](#list-sources)
- [`list-events`](#list-events) _(placeholder)_
- [`batch viirs run`](#batch-viirs-run)

## Global options

These apply to every subcommand and must appear **before** the command name.

| Option                 | Default | Description                                           |
| ---------------------- | ------- | ----------------------------------------------------- |
| `--verbose`, `-v`      | off     | Enable verbose debug logging (loguru, level `DEBUG`). |
| `--install-completion` | —       | Install shell completion for the current shell.       |
| `--show-completion`    | —       | Print completion script (to copy or customise).       |
| `--help`               | —       | Show top-level help and exit.                         |

Example: `uv run atlantis --verbose fetch --event ...`

## Command summary

| Command                   | Purpose                                                                 | Status      |
| ------------------------- | ----------------------------------------------------------------------- | ----------- |
| `setup`                   | Bootstrap data assets and credentials.                                  | implemented |
| `demo`                    | Run the Valencia 2024 end-to-end VIIRS example.                         | implemented |
| `fetch`                   | Fetch raw inundation data for an explicit `--bbox` + date window.       | implemented |
| `fetch-kurosiwo-viirs`    | Fetch VIIRS for KuroSiwo cases (catalogue or precomputed metadata CSV). | implemented |
| `fetch-kurosiwo-modis`    | Fetch MODIS for KuroSiwo cases (catalogue or precomputed metadata CSV). | implemented |
| `build-kurosiwo-metadata` | Derive the KuroSiwo metadata CSV from the GeoPackage catalogue.         | implemented |
| `harmonise`               | Resample fetched outputs to a uniform 1 arcmin grid with normalisation. | implemented |
| `archive`                 | Write harmonised data to Zarr (raw + ML-ready).                         | placeholder |
| `validate`                | Validate archive integrity (optionally with ML smoke test).             | placeholder |
| `list-sources`            | List all registered data sources.                                       | implemented |
| `list-events`             | List events in the archive.                                             | placeholder |
| `batch viirs run`         | Batch-process the VIIRS JPSS catalogue → 1 arcmin COGs on S3 via Dask.  | implemented |

## `setup`

Bootstrap required data assets (VIIRS AOI grid, KuroSiwo catalogue) and
optionally prompt for missing credentials (e.g. `EARTHDATA_TOKEN`).

```bash
uv run atlantis setup [OPTIONS]
```

| Option              | Default | Description                                                                               |
| ------------------- | ------- | ----------------------------------------------------------------------------------------- |
| `--check-only`      | off     | Verify assets/credentials are present without modifying anything.                         |
| `--update-hashes`   | off     | Recompute SHA-256 hashes and write them to `config/asset_hashes.json`.                    |
| `--non-interactive` | off     | Never prompt for missing credentials (default: prompt when stdin is a TTY).               |
| `--verify-aws`      | off     | After standard checks, run a live S3 `ListObjectsV2` against each registered AWS profile. |

Exit code is non-zero when any required asset or credential is missing
(or when `--verify-aws` fails).

## `demo`

Self-contained Valencia 2024 (Spain DANA) end-to-end VIIRS example.
Convenient smoke test after `setup`.

```bash
uv run atlantis demo [OPTIONS]
```

| Option                       | Default              | Description                                         |
| ---------------------------- | -------------------- | --------------------------------------------------- |
| `--output`, `-o`             | `data/Valencia_2024` | Output directory.                                   |
| `--harmonise/--no-harmonise` | `--harmonise`        | Harmonise the peak-flood date to 1 arcmin.          |
| `--stream/--no-stream`       | `--stream`           | Stream remote tiles via `/vsicurl/` (vs. download). |

## `fetch`

Fetch raw inundation data for a single flood event defined by `--bbox`
and a date window. Supports VIIRS, MODIS, GFM (and the planned RFM)
through a single interface; use `--source` to pick one or `all`.

```bash
uv run atlantis fetch [OPTIONS]
```

### Event selection

| Option           | Required | Description                                                          |
| ---------------- | -------- | -------------------------------------------------------------------- |
| `--event`, `-e`  | yes      | Flood event ID (free-form string used in output filenames).          |
| `--source`, `-s` | no       | `gfm`, `viirs`, `modis`, `rfm`, or `all` (default: `all`).           |
| `--output`, `-o` | no       | Output directory for raw data (default: `<cache_dir>/raw/<event>/`). |
| `--bbox`         | yes\*    | Bounding box `"west south east north"` (space-separated, EPSG:4326). |
| `--start-date`   | yes\*    | Start date `YYYY-MM-DD`.                                             |
| `--end-date`     | yes\*    | End date `YYYY-MM-DD`.                                               |

\* `--bbox`, `--start-date`, and `--end-date` must be provided together;
no catalogue lookup is implemented for the generic `fetch` command.

### Output controls (all sources)

| Option                                 | Default                    | Description                                                                                                                                                                                                                                              |
| -------------------------------------- | -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--classify/--no-classify`             | `--classify`               | Classify pixels into flood-fraction / quality-mask / permanent-water layers (MODIS adds recurring-flood). `--no-classify` writes raw integer codes. For GFM, `--no-classify` writes the native `ensemble_flood_extent` and `reference_water_mask` bands. |
| `--stream/--no-stream`                 | `--stream`                 | Stream tiles via GDAL `/vsicurl/` vs. download to disk. MODIS: only valid with `--modis-backend lance_geotiff`. **Ignored for GFM** (always streams via STAC/COG).                                                                                       |
| `--plot`                               | off                        | Save a PNG of the peak-flood date (VIIRS / MODIS / GFM).                                                                                                                                                                                                 |
| `--plot-dir`                           | `<output>/<source>/plots/` | Directory for PNG output.                                                                                                                                                                                                                                |
| `--harmonise`                          | off                        | Reproject the source-resolution `processed/` output (VIIRS 375 m, MODIS 250 m, GFM ~80 m) to the canonical 1-arcmin grid. Classified flood fractions use `average` resampling (uint8 %); native/raw code bands use nearest-neighbour.                    |
| `--keep-processed/--no-keep-processed` | `--keep-processed`         | Write intermediate processed/ GeoTIFFs. `--no-keep-processed` saves disk.                                                                                                                                                                                |

### Multi-date strategy (VIIRS / MODIS / GFM)

| Option               | Default | Description                                                                                                |
| -------------------- | ------- | ---------------------------------------------------------------------------------------------------------- |
| `--strategy`         | `peak`  | `peak` (best flood date), `aggregate` (mean/mode composite), or `all` (one output per date).               |
| `--peak-days-before` | `0`     | Filter dates to this many days **before** the computed peak (≥0). `0` = no filtering.                      |
| `--peak-days-after`  | `0`     | Filter dates to this many days **after** the computed peak (≥0). `0` = no filtering.                       |
| `--peak-window-days` | `0`     | Symmetric shorthand: set both before/after to this value. Cannot be combined with the asymmetric flags.    |
| `--max-observations` | `0`     | Cap dates returned after windowing. `0` = no limit.                                                        |
| `--peak-priority`    | `post`  | Subsampling bias when `--max-observations > 0`: `post` (post-event first), `pre`, or `balanced` (±1, ±2…). |

See [viirs/overview.md#peak-window-filtering-and-subsampling](viirs/overview.md#peak-window-filtering-and-subsampling)
and [viirs/pipeline.md#strategies-in-detail-pixel-level](viirs/pipeline.md#strategies-in-detail-pixel-level)
for pixel-level semantics.

### VIIRS-specific

| Option            | Default   | Description                                                                                 |
| ----------------- | --------- | ------------------------------------------------------------------------------------------- |
| `--viirs-backend` | `noaa_s3` | `noaa_s3` (2012–2020, 2023–2026) or `gmu_legacy` (covers 2021–2022 gap; intermittent host). |
| `--viirs-format`  | `tif`     | `tif`, `netcdf`, `shapezip`, `png`. Only `tif` is implemented.                              |

### MODIS-specific

| Option              | Default         | Description                                                                         |
| ------------------- | --------------- | ----------------------------------------------------------------------------------- |
| `--modis-backend`   | `lance_geotiff` | `lance_geotiff` (streamable, ~1-week NRT) or `laads_hdf4` (download, 2003+).        |
| `--modis-composite` | `F2`            | MCDWD composite: `F1`, `F1C`, `F2`, `F3`. Default `F2` = 2-day max-water composite. |

### GFM-specific

| Option                 | Default   | Description                                                                        |
| ---------------------- | --------- | ---------------------------------------------------------------------------------- |
| `--gfm-coarsen-factor` | `4`       | Spatial coarsening factor applied before reprojection.                             |
| `--gfm-resampling`     | `average` | Resampling method for reprojection to EPSG:4326 (any `rasterio.enums.Resampling`). |

## `fetch-kurosiwo-viirs`

Convenience wrapper around `fetch` for KuroSiwo cases — bbox and date
range are auto-resolved from the bundled GeoPackage catalogue (or from a
precomputed metadata CSV).

```bash
uv run atlantis fetch-kurosiwo-viirs [OPTIONS]
```

### Case selection

| Option           | Default                     | Description                                                                        |
| ---------------- | --------------------------- | ---------------------------------------------------------------------------------- |
| `--metadata`     | —                           | Path to a precomputed KuroSiwo metadata CSV (takes precedence over `--catalogue`). |
| `--catalogue`    | `assets/ks_catalogue.gpkg`  | KuroSiwo GeoPackage catalogue (used when `--metadata` is not supplied).            |
| `--case`         | —                           | Restrict to one KuroSiwo `flood_case` (e.g. `KuroSiwo_1111004`).                   |
| `--limit`        | —                           | Process only the first N cases after filtering.                                    |
| `--output`, `-o` | `<cache_dir>/raw/kurosiwo/` | Output root directory. Each case writes to `<output>/<case>/viirs/`.               |

### Search window

| Option                 | Default | Description                                                                                         |
| ---------------------- | ------- | --------------------------------------------------------------------------------------------------- |
| `--days-before`        | `0`     | Days **before** the KuroSiwo `date_end` to include in the VIIRS search.                             |
| `--days-after`         | `0`     | Days **after** the KuroSiwo `date_end` to include in the VIIRS search.                              |
| `--use-metadata-range` | off     | Use the full `date_start..date_end` from the metadata instead of a narrow window around `date_end`. |

### VIIRS backend & output (same semantics as `fetch`)

`--viirs-backend`, `--viirs-format`, `--classify/--no-classify`,
`--stream/--no-stream`, `--plot`, `--plot-dir`, `--harmonise`,
`--keep-processed/--no-keep-processed`.

Per-case summary is printed as a Rich table; failures across cases are
collected and the process exits non-zero if any case raises.

## `fetch-kurosiwo-modis`

Same shape as `fetch-kurosiwo-viirs` but using the MODIS MCDWD product.

```bash
uv run atlantis fetch-kurosiwo-modis [OPTIONS]
```

### Case selection & search window

Same flags as `fetch-kurosiwo-viirs`: `--metadata`, `--catalogue`,
`--case`, `--limit`, `--output`, `--days-before`, `--days-after`,
`--use-metadata-range`.

### MODIS-specific options

| Option                                 | Default            | Description                                                          |
| -------------------------------------- | ------------------ | -------------------------------------------------------------------- |
| `--modis-backend`                      | `lance_geotiff`    | `lance_geotiff` (streamable, NRT) or `laads_hdf4` (download, 2003+). |
| `--modis-composite`                    | `F2`               | `F1`, `F1C`, `F2`, `F3`.                                             |
| `--classify/--no-classify`             | `--classify`       | Classify into flood / recurring / permanent / quality layers.        |
| `--stream/--no-stream`                 | `--stream`         | Stream via `/vsicurl/` (only with `--modis-backend lance_geotiff`).  |
| `--plot`, `--plot-dir`, `--harmonise`  | —                  | As in `fetch`.                                                       |
| `--keep-processed/--no-keep-processed` | `--keep-processed` | Keep intermediate processed/ GeoTIFFs.                               |

## `build-kurosiwo-metadata`

Derive the KuroSiwo metadata CSV from the GeoPackage catalogue. The CSV
is faster to re-read than the full GeoPackage and is the recommended
input for repeated `--metadata` runs.

```bash
uv run atlantis build-kurosiwo-metadata [OPTIONS]
```

| Option        | Default                                  | Description                           |
| ------------- | ---------------------------------------- | ------------------------------------- |
| `--catalogue` | `assets/ks_catalogue.gpkg`               | Source KuroSiwo GeoPackage catalogue. |
| `--output`    | `data/metadata/kurosiwo_metadata_v1.csv` | Destination CSV path.                 |

## `harmonise`

Standalone harmonisation step: reproject + normalise already-fetched
processed GeoTIFFs to a uniform 1 arcmin grid. Supports VIIRS and MODIS
inputs (looks for files matching `{event}_*_{source}_flood_fraction.tif`
or `{event}_*_{source}_raw.tif`).

```bash
uv run atlantis harmonise [OPTIONS]
```

| Option                | Required | Default                           | Description                                         |
| --------------------- | -------- | --------------------------------- | --------------------------------------------------- |
| `--event`, `-e`       | yes      | —                                 | Flood event ID (used to match input filenames).     |
| `--source`, `-s`      | yes      | —                                 | Data source ID (`viirs` or `modis`).                |
| `--input`, `-i`       | no       | `<cache_dir>/raw/<event>/`        | Input directory with fetched/processed data.        |
| `--output`, `-o`      | no       | `<cache_dir>/harmonised/<event>/` | Output directory for harmonised GeoTIFFs.           |
| `--target-resolution` | no       | `0.01667` (1 arcmin)              | Target spatial resolution in degrees.               |
| `--resampling`        | no       | `average`                         | Resampling method for `flood_fraction`.             |
| `--dry-run`           | no       | off                               | Print what would be done without writing any files. |

The command searches the standard `…/<source>/processed/` layout first,
falling back to a broader `rglob` (including the KuroSiwo
`<case>/<source>/processed/` layout). It exits non-zero if no matching
files are found.

## `archive`

> Placeholder — archive writing is not yet implemented.

```bash
uv run atlantis archive [OPTIONS]
```

| Option            | Default                 | Description                             |
| ----------------- | ----------------------- | --------------------------------------- |
| `--event`, `-e`   | required                | Flood event ID to archive.              |
| `--source`, `-s`  | all available           | Data source.                            |
| `--archive`, `-a` | `<config.archive_root>` | Archive root directory.                 |
| `--raw-only`      | off                     | Only write raw archive (skip ML-ready). |

## `validate`

> Placeholder — archive validation is not yet implemented.

```bash
uv run atlantis validate [OPTIONS]
```

| Option            | Default                 | Description                                  |
| ----------------- | ----------------------- | -------------------------------------------- |
| `--event`, `-e`   | all events              | Event ID to validate.                        |
| `--source`, `-s`  | all sources             | Source ID to validate.                       |
| `--archive`, `-a` | `<config.archive_root>` | Archive root directory.                      |
| `--check-ml`      | off                     | Also run ML validation (PyTorch smoke test). |

## `list-sources`

List all registered data sources (via the fetcher registry).

```bash
uv run atlantis list-sources
```

## `list-events`

> Placeholder — archive event listing is not yet implemented.

```bash
uv run atlantis list-events [OPTIONS]
```

| Option            | Default                 | Description             |
| ----------------- | ----------------------- | ----------------------- |
| `--archive`, `-a` | `<config.archive_root>` | Archive root directory. |

## `batch viirs run`

Batch-process the VIIRS JPSS 2020 catalogue into 1-arcmin uint8
flood-fraction COGs on `s3://atlantis/` via a local Dask cluster.
Progress is persisted in a SQLite tracker DB so runs can be safely
interrupted and resumed.

```bash
uv run atlantis batch viirs run [OPTIONS]
```

| Option             | Default                                                  | Description                                                   |
| ------------------ | -------------------------------------------------------- | ------------------------------------------------------------- |
| `--inventory`      | `s3://atlantis/assets/viirs/jpss/2020/catalogue.parquet` | Path or S3 URI to the VIIRS JPSS catalogue Parquet file.      |
| `--output`         | `s3://atlantis/viirs/jpss/2020/`                         | S3 prefix for output COGs (must start with `s3://atlantis/`). |
| `--partition`      | full catalogue                                           | Row slice of the catalogue, e.g. `0:24464`.                   |
| `--workers-min`    | `2`                                                      | Minimum Dask worker processes.                                |
| `--workers-max`    | `6`                                                      | Maximum Dask worker processes (adaptive).                     |
| `--memory-limit`   | `6GB`                                                    | Memory cap per worker.                                        |
| `--dashboard-port` | `8787`                                                   | Dask dashboard port.                                          |
| `--db-path`        | `tracker.db`                                             | SQLite resume database path.                                  |
| `--retries`        | `3`                                                      | Dask retry count per granule.                                 |
| `--log-every`      | `100`                                                    | Log a progress line every N completions.                      |

**Two-VM split example:**

```bash
# VM1
uv run atlantis batch viirs run --partition 0:24464 --db-path tracker_vm1.db

# VM2
uv run atlantis batch viirs run --partition 24464:48928 --db-path tracker_vm2.db
```

Pre-flight checks: `--output` must start with `s3://atlantis/`, and the
`default` AWS profile must be configured (run `atlantis setup` first).

See [batch/viirs/jpss.md](batch/viirs/jpss.md) for operational notes.
