# Atlantis — Architecture Guide

> **What is Atlantis?** An evolving flood-observation pipeline that fetches raw EO flood data from multiple sources (VIIRS, GFM, MODIS), harmonises it to a common 1 arcmin grid, and will eventually archive ML-ready products. Three source paths are fully operational with harmonisation support.

---

## 1. Current State

Atlantis has a strong domain model, fetcher registry, and three fully operational source paths with harmonisation support.

Across all three sources, Atlantis now distinguishes between **native layers**
(the source products fetched unchanged, emitted by `--no-classify`) and
**derived layers** (computed by Atlantis, emitted by `--classify`). The shared
layer registries under `src/atlantis/layers/` drive CLI discovery, dataset
construction, and the generated layer catalogue in `docs/layers.md`.

### Implemented today

- `VIIRSFetcher` can search, download, mosaic, clip, and write GeoTIFF outputs from the NOAA S3 or GMU JPSS Flood archive.
- `GFMFetcher` can search, stream, coarsen, reproject, and write GeoTIFF outputs from the EODC STAC API (Sentinel-1 SAR). The exact GFM native/derived inventory is centralised in `docs/layers.md`.
- `MODISFetcher` can search, download/stream, mosaic, clip, and write GeoTIFF outputs from NASA LANCE (NRT) or LAADS (archive) backends.
- `atlantis fetch` works for explicit bbox/date extraction across all three sources (VIIRS, GFM, MODIS).
- `atlantis fetch-kurosiwo-viirs` and `atlantis fetch-kurosiwo-modis` work directly from the KuroSiwo catalogue or from a precomputed metadata CSV.
- `atlantis build-kurosiwo-metadata` derives the metadata CSV from the catalogue without using the notebook.
- `atlantis harmonise` reprojects and normalises fetched data to a uniform 1 arcmin grid.
- `atlantis setup` bootstraps required data assets and credentials.
- `atlantis demo` (VIIRS), `atlantis demo-modis`, and `atlantis demo-gfm` each run a self-contained Valencia 2024 example end to end.
- All three fetchers expose `last_diagnostics` (`SearchDiagnostics` / `ModisSearchDiagnostics` / `GfmSearchDiagnostics`) so the CLI can surface actionable hints when a fetch returns no results.
- `atlantis batch viirs run` processes VIIRS granules in bulk using Dask, writing 1-arcmin COGs to S3.
- `download_file()` is implemented and reuses existing files.
- KuroSiwo metadata can be converted into `FloodEvent` objects through `utils.kurosiwo`.
- Harmoniser (Reprojector + Normaliser) is fully operational and used by the `--harmonise` CLI flag.

### Still planned / mostly stubbed

- RFM fetcher (Phase C)
- Tiler (uniform grid tiling for ML)
- Raw and ML-ready Zarr archive writing
- Archive validation and ML smoke-test pipeline

The rest of this document reflects both the target architecture and the current implementation status.

---

## 2. Pipeline At A Glance

```
┌─────────────────────────────────────────────────────────────────┐
│                         FLOOD EVENT                             │
│   event_id · bbox · start_date · end_date · sources             │
└──────────────┬──────────────────────────────────────┬───────────┘
               │                                      │
               ▼                                      │
┌──────────────────────────────┐                       │
│           FETCHERS           │                       │
│  AbstractFloodFetcher +      │                       │
│  @register_fetcher registry  │                       │
│                              │                       │
│  GFMFetcher   ──► STAC/EODC  │  implemented          │
│  VIIRSFetcher ──► JPSS Flood │  implemented          │
│  MODISFetcher ──► LANCE/LAADS│  implemented          │
│  RFMFetcher   ──► Phase C    │  planned              │
└──────────────┬───────────────┘                       │
               │ raw raster files (GeoTIFF, ZIP, …)    │
               ▼                                        │
┌──────────────────────────────┐                       │
│         HARMONISER           │                       │
│  Reprojector ──► Normaliser  │  implemented          │
│  Tiler                       │  planned              │
│                              │                       │
│  Target: EPSG:4326, 1 arcmin │                       │
│  grid, values → [0, 1]       │                       │
└──────────────┬───────────────┘                       │
               │ harmonised xarray Datasets            │
               ▼                                       │
┌──────────────────────────────┐                       │
│           ARCHIVE            │                       │
│                              │                       │
│  ArchiveWriter               │                       │
│    └─► raw/                  │  planned              │
│    └─► ml-ready/             │  planned              │
│                              │                       │
│  ArchiveReader               │                       │
│                              │                       │
│  Checkpoint markers (.done)  │  implemented          │
└──────────────┬───────────────┘                       │
               │                                       │
               ▼                                       │
┌──────────────────────────────┐                       │
│          VALIDATION          │                       │
│                              │                       │
│  ArchiveChecker              │  planned              │
│  MLLoaderValidator           │  planned              │
└──────────────────────────────────────────────────────┘
```

---

## 3. Package Layout

```
src/atlantis/
├── __init__.py            # Package entry point, version
├── cli.py                 # Typer CLI — fetch / fetch-kurosiwo-viirs / fetch-kurosiwo-modis / harmonise / etc.
├── config.py              # Pydantic settings hierarchy (env-var driven)
├── layers/
│   ├── spec.py            # NativeLayer / DerivedLayer / DerivationContext
│   ├── registry.py        # LayerRegistry + cross-source discovery helpers
│   └── docs.py            # Markdown renderer for docs/layers.md
│
├── models/
│   ├── event.py           # FloodEvent dataclass
│   └── metadata.py        # TileMetadata / SourceMetadata (Pydantic)
│
├── fetchers/
│   ├── base.py            # AbstractFloodFetcher ABC + SearchResult / FetchResult
│   ├── registry.py        # @register_fetcher decorator + global registry
│   ├── rfm.py             # RFMFetcher — planned / Phase C stub
│   ├── _dataset.py        # Shared dataset utility helpers
│   ├── gfm/
│   │   ├── __init__.py    # GFMFetcher + GfmSearchDiagnostics — implemented
│   │   ├── backend.py     # GfmStacBackend (EODC STAC API)
│   │   ├── processor.py   # GfmRasterProcessor (coarsen → reproject → accumulate)
│   │   ├── dataset.py     # GFM tile → xarray Dataset conversion
│   │   ├── selection.py   # Peak-window filtering and subsampling
│   │   └── README.md      # Module guide
│   ├── modis/
│   │   ├── __init__.py    # MODISFetcher + ModisSearchDiagnostics — implemented
│   │   ├── backend.py     # LanceGeotiffBackend, LaadsHdf4Backend
│   │   ├── processor.py   # ModisRasterProcessor (mosaic, clip, classify)
│   │   ├── dataset.py     # MODIS tile → xarray Dataset conversion
│   │   ├── selection.py   # Peak-window filtering and subsampling
│   │   └── README.md      # Module guide
│   └── viirs/
│       ├── __init__.py    # VIIRSFetcher + SearchDiagnostics — implemented
│       ├── backend.py     # Backend classes (NoaaS3Backend, GmuLegacyBackend)
│       ├── processor.py   # Raster processing (ViirsRasterProcessor)
│       ├── dataset.py     # VIIRS tile → xarray Dataset conversion
│       ├── selection.py   # Peak-window filtering and subsampling
│       ├── inventory.py   # VIIRS JPSS batch catalogue loader + task builder
│       ├── batch_processor.py  # Per-granule Dask batch processing function
│       ├── README.md      # Module guide
│       └── data/
│           └── viirs_aois.geojson  # Packaged VIIRS AOI tile grid
│
├── batch/
│   ├── __init__.py        # BatchConfig, TaskResult, run_batch — Dask batch engine
│   ├── config.py          # BatchConfig (Pydantic settings: workers, S3 paths, etc.)
│   ├── orchestrator.py    # Dask LocalCluster run loop (submit → drain → checkpoint)
│   └── tracker.py         # SQLite progress tracker (init_db, mark_done, mark_failed)
│
├── harmoniser/
│   ├── __init__.py        # Harmoniser orchestrator + write_harmonised_raster
│   ├── reprojector.py     # CRS reprojection + grid-snapped resampling (implemented)
│   ├── tiler.py           # Uniform grid tiling (planned)
│   └── normaliser.py      # Value scaling + quality mask generation (implemented)
│
├── archive/
│   ├── writer.py          # Planned raw / ml-ready Zarr writing + working checkpoints
│   └── reader.py          # Planned archive reader
│
├── stac/
│   ├── stac_api.py        # STAC API utilities
│   └── stac_catalog.py    # STAC catalog helpers
│
├── validation/
│   ├── checker.py         # Planned archive checks
│   └── ml_loader.py       # Planned PyTorch smoke tests
│
└── utils/
    ├── geo.py            # BBox dataclass + validation / intersection helpers
    ├── io.py             # ensure_dir / get_cache_path / download_file
    ├── kurosiwo.py       # Derive KuroSiwo metadata and build FloodEvent objects
    ├── plot.py           # Visualisation (flood maps, pixel stats, PNG output)
    ├── setup.py          # Asset/credential bootstrapping for `atlantis setup`
    └── ui.py             # Rich console formatting (progress bars, tables, status)
```

---

## 4. Core Abstractions

### 4.1 `FloodEvent`

```python
@dataclass
class FloodEvent:
    event_id: str
    bbox: tuple[float, float, float, float]  # (west, south, east, north)
    start_date: date
    end_date: date
    sources: list[str]
```

`FloodEvent` is still the fundamental unit of work. It captures a region, a time window, and a set of sources. All fetcher paths (VIIRS, GFM, MODIS) and the KuroSiwo helper converge on this type.

### 4.2 `AbstractFloodFetcher`

Sources are discovered through the registry rather than hardcoded branch logic.

To add a new source:

1. Subclass `AbstractFloodFetcher`
2. Implement `search()` and `fetch()`
3. Optionally implement `to_dataset()`
4. Register with `@register_fetcher("name")`

That keeps the CLI and pipeline entrypoints source-agnostic.

### 4.3 `FetchResult` and `TileMetadata`

The fetch layer returns `FetchResult` objects that point at written files plus `TileMetadata` describing provenance such as bbox, resolution, CRS, and cloud fraction.

For all implemented fetchers (VIIRS, GFM, MODIS), one `FetchResult` corresponds to one processed date.

### 4.4 Dual-archive strategy

The intended long-term design still distinguishes:

| Archive     | Purpose                                         | Status  |
| ----------- | ----------------------------------------------- | ------- |
| `raw/`      | Preservation of fetched source data             | planned |
| `ml-ready/` | Harmonised, tiled, normalised training products | planned |

This strategy is still valid architecturally, but only the fetch and harmonise layers are operational today.

### 4.5 Checkpoints

`ArchiveWriter.write_checkpoint()` and `is_checkpointed()` already exist and are accurate to the design. Checkpointing is one of the few implemented pieces in the archive layer.

---

## 5. Working VIIRS Flow

This section consolidates the operational material that used to live in the separate VIIRS document.

### 5.1 What You Need

- A checked out copy of this repository
- `uv` installed
- Geo dependencies installed
- Network access (VIIRS: NOAA S3 or GMU; GFM: EODC STAC; MODIS: NASA LANCE/LAADS)
- For MODIS: `EARTHDATA_TOKEN` environment variable (register at <https://urs.earthdata.nasa.gov/>)
- For KuroSiwo runs: either the catalogue at `assets/ks_catalogue.gpkg` or a precomputed metadata CSV

Install the required dependencies and bootstrap assets with:

```bash
uv sync --extra geo
uv run atlantis setup
```

### 5.2 How The VIIRS Fetcher Works

For each requested date and bbox, `VIIRSFetcher`:

1. Loads the packaged global AOI grid from `fetchers/viirs/data/viirs_aois.geojson`
2. Selects AOI tiles intersecting the requested bbox
3. Looks up matching archive entries under the JPSS Flood date directory
4. Downloads ZIP tiles and reuses existing downloads on rerun
5. Extracts TIFFs, mosaics them, and clips them to the bbox
6. Writes three processed GeoTIFF outputs

### 5.3 Explicit Region CLI Usage

Use this for direct bbox/date extraction. Tiles are streamed from NOAA S3 and flood layers are classified by default. The recommended invocation uses the default `peak` strategy;
add `--no-keep-processed` to skip writing intermediate 375 m files, or `--strategy aggregate` to return a temporal mean/mode composite:

```bash
uv run atlantis fetch \
  --event Yangtze_2020 \
  --source viirs \
  --bbox "105 28 125 38" \
  --start-date 2020-07-22 \
  --end-date 2020-07-22 \
  --no-keep-processed --harmonise
```

This writes only:

```text
~/.cache/atlantis/raw/Yangtze_2020/
└── viirs/
    ├── harmonised/
    │   └── Yangtze_2020_2020-07-22_viirs_harmonised.tif
    └── plots/
        └── harmonised/
            └── derived/
                └── Yangtze_2020_2020-07-22_viirs_harmonised.png
```

Without any optional flags (still streams and classifies by default, writes all intermediate files):

```bash
uv run python -m atlantis.cli fetch \
  --event Yangtze_2020 \
  --source viirs \
  --bbox "105 28 125 38" \
  --start-date 2020-07-22 \
  --end-date 2020-07-22
```

Expected console shape:

```text
Fetching data for event: Yangtze_2020
Sources: viirs
Output: ~/.cache/atlantis/raw/Yangtze_2020

Fetching from viirs...
  Wrote 3 files
```

Default output layout (no flags, streaming and classify on):

```text
~/.cache/atlantis/raw/Yangtze_2020/
└── viirs/
    └── processed/
        ├── Yangtze_2020_20200722_viirs_<layer>.tif
        └── ... one file per derived VIIRS layer from docs/layers.md
```

Use `--no-stream` to cache raw tiles to disk for reuse across runs. Use `--no-classify` to emit the native source layer (`raw` for VIIRS and MODIS; native SAR bands for GFM) instead of Atlantis-derived layers.

### 5.4 KuroSiwo CLI Usage

There are now two supported KuroSiwo paths.

Build the metadata CSV directly from the catalogue:

```bash
uv run python -m atlantis.cli build-kurosiwo-metadata \
  --catalogue assets/ks_catalogue.gpkg \
  --output data/metadata/kurosiwo_metadata_v1.csv
```

Use this when you want a reusable metadata artifact without running a notebook. The CLI default now writes to `data/metadata/kurosiwo_metadata_v1.csv`.

Recommended invocation — tiles are streamed and classified by default; add `--no-keep-processed` to skip writing intermediate 375 m files, or `--strategy aggregate` to return a temporal mean/mode composite:

```bash
uv run python -m atlantis.cli fetch-kurosiwo-viirs \
  --catalogue assets/ks_catalogue.gpkg \
  --case KuroSiwo_470 \
  --no-keep-processed --harmonise
```

Fetch VIIRS directly from the catalogue without `--no-keep-processed` if you need 375 m intermediates for later processing:

```bash
uv run python -m atlantis.cli fetch-kurosiwo-viirs \
  --catalogue assets/ks_catalogue.gpkg \
  --case KuroSiwo_470 \
  --days-before 0 \
  --days-after 0
```

Expected console shape:

```text
KuroSiwo metadata: derived from assets/ks_catalogue.gpkg
Cases selected: 1
Output root: ~/.cache/atlantis/raw/kurosiwo

Fetching KuroSiwo_470 (2020-10-14 -> 2020-10-14)
  Wrote 3 files

Total files written: 3
```

You can still fetch from a precomputed metadata CSV when you want a stable intermediate artifact:

```bash
uv run python -m atlantis.cli fetch-kurosiwo-viirs \
  --metadata data/metadata/kurosiwo_metadata_v1.csv \
  --case KuroSiwo_470 \
  --days-before 0 \
  --days-after 0
```

Default output layout (without `--no-keep-processed`, streaming on by default):

```text
~/.cache/atlantis/raw/kurosiwo/
└── KuroSiwo_470/
    └── viirs/
        ├── raw/
        └── processed/
            ├── KuroSiwo_470_20201014_viirs_<layer>.tif
            └── ... one file per derived VIIRS layer from docs/layers.md
```

Widen the search window around the KuroSiwo flood-time date:

```bash
uv run python -m atlantis.cli fetch-kurosiwo-viirs \
  --metadata data/metadata/kurosiwo_metadata_v1.csv \
  --case KuroSiwo_470 \
  --days-before 2 \
  --days-after 2
```

Use the full KuroSiwo metadata range instead:

```bash
uv run python -m atlantis.cli fetch-kurosiwo-viirs \
  --metadata data/metadata/kurosiwo_metadata_v1.csv \
  --case KuroSiwo_470 \
  --use-metadata-range
```

That mode is intentionally not the default because KuroSiwo `date_start -> date_end` often spans long SAR baseline windows and can trigger many daily VIIRS archive checks.

### 5.5 KuroSiwo Metadata Mapping

`utils.kurosiwo` derives metadata directly from the GeoPackage and interprets the resulting event table as:

- `flood_case` → `FloodEvent.event_id`
- `(lon_min, lat_min, lon_max, lat_max)` → `(west, south, east, north)` bbox
- Default time window → `date_end` only
- Optional widened window → `date_end ± N days`
- Optional full-range mode → `date_start .. date_end`

### 5.6 VIIRS Output Semantics

With `--classify` (default), Atlantis writes one GeoTIFF per derived VIIRS
layer from the canonical layer reference in `docs/layers.md`. The fraction
layers are written as uint8 percentages; the categorical masks remain uint8.

With `--no-classify`, Atlantis writes the single native `raw` VFM layer instead.
The native source `_FillValue` is `1`; Atlantis also treats `0` as missing when
clip/mosaic operations introduce empty pixels.

These outputs are suitable for analysis and for conversion through `to_dataset()`, and can be harmonised to 1 arcmin via `--harmonise`.

### 5.7 Current VIIRS Limitations

- Only the non-NRT JPSS archive is supported
- VIIRS currently writes GeoTIFF outputs, not Zarr
- The KuroSiwo metadata build step is now available in `src/`, and the default derived CSV path now lives under `data/metadata/` rather than notebook drafts.

---

## 6. Working GFM Flow

### 6.1 How The GFM Fetcher Works

`GFMFetcher` accesses Sentinel-1 SAR-derived flood extent data via the EODC STAC API:

1. Searches STAC items intersecting the bbox and time window
2. Streams Cloud-Optimised GeoTIFFs on the fly (no download step)
3. Coarsens (max-pool) by a configurable factor (default 4×)
4. Reprojects to EPSG:4326 aligned to the canonical 1-arcmin global grid
5. Accumulates all dates into per-date or aggregated outputs

> **Note:** Unlike VIIRS and MODIS, GFM reprojects to the target 1-arcmin grid
> _during_ fetch (because the native ~20 m SAR data is too large to materialise).
> When `--harmonise` is used, the reprojection step is effectively a no-op since
> the data is already grid-aligned. The normaliser still runs to scale values.

### 6.2 GFM CLI Usage

```bash
uv run atlantis fetch \
  --event Yangtze_2020 \
  --source gfm \
  --bbox "105 28 125 38" \
  --start-date 2020-07-20 \
  --end-date 2020-07-25 \
  --harmonise
```

Key GFM-specific options:

- `--gfm-coarsen-factor N` — spatial coarsening before reprojection (default 4)
- `--gfm-resampling METHOD` — resampling for reprojection (default `average`)
- `--strategy peak|aggregate|all` — date selection

### 6.3 GFM Output Semantics

With `--classify` (default), Atlantis writes one GeoTIFF per derived or
code-preserving companion layer listed in the canonical GFM section of
`docs/layers.md`.

With `--no-classify` (native/raw mode — emits SAR band codes as-is):

- `*_gfm_ensemble_flood_extent.tif` — uint8 flood code (0=dry, 1=flood, 255=nodata)
- `*_gfm_reference_water_mask.tif` — uint8 water code (0=land, 1=water, 2=permanent, 255=nodata)

Atlantis currently exposes these two native GFM layers. The upstream STAC items
include additional assets, but they are not yet surfaced by the fetcher.

In native mode, reprojection uses nearest-neighbour; the coarsen step is skipped.

---

## 7. Working MODIS Flow

### 7.1 How The MODIS Fetcher Works

`MODISFetcher` accesses NASA MCDWD flood detection products at ~250 m:

1. Determines MODIS sinusoidal tile (h, v) coverage for the bbox
2. Queries either the LANCE NRT mirror (last ~1 week) or the LAADS archive (2003+)
3. Downloads or streams composites (F1, F1C, F2, F3)
4. Mosaics tiles, clips to the bbox, and either emits the native `raw` composite or derives Atlantis layers from it
5. Writes processed GeoTIFF outputs

### 7.2 MODIS Backends

| Backend         | Coverage                 | Access mode            | Auth              |
| --------------- | ------------------------ | ---------------------- | ----------------- |
| `lance_geotiff` | NRT (~last week)         | Stream via `/vsicurl/` | `EARTHDATA_TOKEN` |
| `laads_hdf4`    | 2003–2025 + archived NRT | Download HDF4          | `EARTHDATA_TOKEN` |

### 7.3 MODIS CLI Usage

```bash
uv run atlantis fetch \
  --event Valencia_2024 \
  --source modis \
  --bbox "-1.5 38.8 0.5 40.0" \
  --start-date 2024-10-29 \
  --end-date 2024-11-04 \
  --modis-backend laads_hdf4 \
  --modis-composite F2 \
  --harmonise
```

KuroSiwo extraction:

```bash
uv run atlantis fetch-kurosiwo-modis \
  --catalogue assets/ks_catalogue.gpkg \
  --case KuroSiwo_470 \
  --modis-backend laads_hdf4 \
  --harmonise
```

### 7.4 MODIS Composites

| Composite | Description                             |
| --------- | --------------------------------------- |
| `F1`      | 1-day flood detection                   |
| `F1C`     | 1-day flood detection (cloud-corrected) |
| `F2`      | 2-day max-water composite (default)     |
| `F3`      | 3-day max-water composite               |

### 7.5 MODIS Output Semantics

When classified (`--classify`, the default):

Atlantis writes one GeoTIFF per derived MODIS layer from the canonical layer
reference in `docs/layers.md`. Fraction layers are written as uint8
percentages; categorical layers remain uint8 masks/codes.

When raw (`--no-classify`):

- `*_modis_raw.tif` — original MCDWD integer pixel codes

Atlantis also knows about the eleven native MODIS count layers present in the
HDF product, but the standard fetch pipeline currently emits the selected `raw`
composite in native mode.

---

## 8. Module Deep Dive

### 8.1 `config.py`

Configuration is centered on `AtlantisConfig`, which combines:

- `HarmoniseConfig`
- `ArchiveConfig`
- `FetcherConfig`

Key fetcher settings:

- `cache_dir` defaults to `~/.cache/atlantis`
- `timeout` defaults to 300 seconds
- `max_retries` defaults to 3
- `gfm_api_url` can override the EODC STAC endpoint
- `gfm_coarsen_factor` controls spatial coarsening (default 4)
- `gfm_resampling` controls the reprojection method (default `average`)
- `viirs_backend` defaults to `noaa_s3`
- `viirs_base_url` can override the NOAA S3 archive base URL
- `viirs_legacy_base_url` can override the GMU legacy base URL
- `modis_backend` defaults to `lance_geotiff`
- `modis_composite` defaults to `F2`
- `modis_lance_primary_base_url` / `modis_lance_backup_base_url` can override LANCE mirrors
- `modis_laads_base_url` can override the LAADS archive URL

Key harmoniser settings:

- `target_crs` defaults to `EPSG:4326`
- `target_resolution` defaults to 1 arcmin (~0.01667°)
- `snap_to_global_grid` aligns outputs to a canonical 1-arcmin reference grid
- `variable_resampling` allows per-variable resampling overrides (flood_fraction→average, masks→mode)

### 8.2 `models/`

- `FloodEvent` is accurate and actively used by all fetchers
- `TileMetadata` is accurate and actively used by VIIRS, MODIS, and GFM
- `SourceMetadata` exists but is not yet central to the working flow

### 8.3 `fetchers/`

| Fetcher        | Data                            | Backend                            | Diagnostics class        | Status      |
| -------------- | ------------------------------- | ---------------------------------- | ------------------------ | ----------- |
| `GFMFetcher`   | SAR flood maps (Sentinel-1)     | STAC/EODC                          | `GfmSearchDiagnostics`   | implemented |
| `VIIRSFetcher` | Archived VIIRS flood composites | NOAA S3 / GMU JPSS Flood archive   | `SearchDiagnostics`      | implemented |
| `MODISFetcher` | MCDWD flood detection (~250 m)  | NASA LANCE (NRT) / LAADS (archive) | `ModisSearchDiagnostics` | implemented |
| `RFMFetcher`   | Modelled flood extent           | Phase C                            | —                        | stub        |

All three implemented fetchers share a consistent interface: `search()`, `fetch()`, and `to_dataset()`. They support peak-window filtering (`--peak-days-before/after`, `--max-observations`, `--peak-priority`) and three strategies (`peak`, `aggregate`, `all`). Each populates `fetcher.last_diagnostics` after `search()` so the CLI can emit actionable guidance on empty fetches.

**Important pipeline difference:** GFM reprojects to the 1-arcmin grid _during_ fetch (the native ~20 m SAR data is too large to write at full resolution). VIIRS and MODIS fetch at native resolution (375 m / ~250 m) and harmonise to 1 arcmin only when explicitly requested via `--harmonise` or `atlantis harmonise`.

### 8.4 `harmoniser/`

The harmonisation pipeline consists of:

1. **Reproject** (implemented) — CRS detection, grid snapping to a canonical 1-arcmin reference grid, per-variable resampling
2. **Normalise** (implemented) — value scaling to [0, 1], quality mask generation
3. **Tile** (planned) — uniform 224×224 tiling for ML models

Layer metadata controls important parts of this step: derived flood fractions
default to averaging, while native code layers and categorical masks use
nearest-neighbour or mode-style handling as declared in the layer registries.

The `Harmoniser` orchestrator class chains Reprojector → Normaliser and is used by:

- The `--harmonise` flag on `fetch` / `fetch-kurosiwo-viirs` / `fetch-kurosiwo-modis`
- The standalone `atlantis harmonise` command

### 8.5 `archive/`

The archive layout and dual raw / ml-ready concept remain the intended design. `ArchiveWriter` currently provides working checkpoint helpers, but raw and ML-ready Zarr writing are still not implemented.

### 8.6 `validation/`

Validation is still planned. The API surface exists (`checker.py`, `ml_loader.py`), but the checks themselves remain placeholders.

### 8.7 `utils/`

- `geo.py` provides bbox validation and geometric helpers
- `io.py` provides directory creation, cache naming, and a working streaming downloader
- `kurosiwo.py` converts KuroSiwo metadata rows into `FloodEvent` objects for extraction
- `plot.py` provides flood map visualisation, pixel statistics, and PNG output
- `setup.py` implements asset/credential bootstrapping for `atlantis setup`
- `ui.py` provides Rich console formatting (progress bars, tables, status indicators)

---

## 9. CLI Commands

| Command                                                                              | Purpose                                                    | Status      |
| ------------------------------------------------------------------------------------ | ---------------------------------------------------------- | ----------- |
| `atlantis fetch --event E --source viirs --bbox ... --start-date ... --end-date ...` | Fetch data for an explicit bbox/date window (any source)   | implemented |
| `atlantis build-kurosiwo-metadata --catalogue ... --output ...`                      | Derive KuroSiwo metadata CSV from the GeoPackage           | implemented |
| `atlantis fetch-kurosiwo-viirs --catalogue ...`                                      | Fetch VIIRS for KuroSiwo cases directly from the catalogue | implemented |
| `atlantis fetch-kurosiwo-viirs --metadata ...`                                       | Fetch VIIRS for KuroSiwo cases from metadata CSV           | implemented |
| `atlantis fetch-kurosiwo-modis --catalogue ...`                                      | Fetch MODIS for KuroSiwo cases directly from the catalogue | implemented |
| `atlantis fetch-kurosiwo-modis --metadata ...`                                       | Fetch MODIS for KuroSiwo cases from metadata CSV           | implemented |
| `atlantis harmonise --event E --source S`                                            | Reproject → normalise fetched data to 1 arcmin             | implemented |
| `atlantis setup`                                                                     | Bootstrap assets and credentials                           | implemented |
| `atlantis demo`                                                                      | Valencia 2024 end-to-end example (VIIRS)                   | implemented |
| `atlantis demo-modis`                                                                | Valencia 2024 end-to-end example (MODIS)                   | implemented |
| `atlantis demo-gfm`                                                                  | Valencia 2024 end-to-end example (GFM)                     | implemented |
| `atlantis list-sources`                                                              | Print registered fetchers and descriptions                 | implemented |
| `atlantis list-events`                                                               | List events present in the cache/archive                   | implemented |
| `atlantis batch viirs run`                                                           | Bulk-process VIIRS granules → 1-arcmin COGs on S3 (Dask)   | implemented |
| `atlantis archive --event E`                                                         | Write raw + ml-ready Zarr archives                         | planned     |
| `atlantis validate --event E`                                                        | Run archive and ML validation checks                       | planned     |

---

## 10. Dependency Groups

```bash
uv sync                           # Core only
uv sync --extra geo               # xarray, zarr, rioxarray, odc-stac, pystac-client, pyproj, shapely, rasterio, requests, beautifulsoup4
uv sync --extra ml                # torch, numpy, scikit-learn, matplotlib
uv sync --extra notebooks         # geo + ml + notebook-only tooling such as earthkit-data, cartopy, metview
```

For the working fetch paths, `uv sync --extra geo` is the relevant setup step. MODIS additionally requires an `EARTHDATA_TOKEN` environment variable.

---

## 11. Extending Atlantis

The extension pattern remains the same: add a fetcher, register it, and keep source-specific logic inside the adapter rather than the CLI.

Minimal example:

```python
from pathlib import Path

from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult, SearchResult
from atlantis.fetchers.registry import register_fetcher
from atlantis.models.event import FloodEvent


@register_fetcher("sentinel2")
class Sentinel2Fetcher(AbstractFloodFetcher):
    source_id: str = "sentinel2"

    def search(self, event: FloodEvent) -> list[SearchResult]:
        ...

    def fetch(self, event: FloodEvent, output_dir: Path) -> list[FetchResult]:
        ...
```

Importing the module in `cli.py` is still enough to expose it through the registry.

---

## 12. Design Principles

| Principle               | How Atlantis implements it                                                                       |
| ----------------------- | ------------------------------------------------------------------------------------------------ |
| Pluggable sources       | `@register_fetcher` decorator + global registry                                                  |
| Config-driven behavior  | Paths, timeouts, CRS, tile settings from config/env                                              |
| Separation of concerns  | Fetchers, harmoniser, archive, validation are distinct layers                                    |
| Type-safe core          | Dataclasses for events, Pydantic models for metadata                                             |
| Resumable processing    | Checkpoint markers in the archive layer                                                          |
| Evolving implementation | Current code can expose partial but useful workflows while preserving the long-term architecture |
