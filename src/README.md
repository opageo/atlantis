# Atlantis — Architecture Guide

> **What is Atlantis?** An evolving flood-observation pipeline that aims to fetch raw EO flood data, harmonise it to a common grid, and eventually archive ML-ready products. Today, the most complete path is VIIRS extraction for explicit regions and KuroSiwo-derived locations.

---

## 1. Current State

Atlantis is part architecture, part implementation. The codebase already has a strong domain model and fetcher registry, but only one source path is currently working end to end in `src/`.

### Implemented today

- `VIIRSFetcher` can search, download, mosaic, clip, and write GeoTIFF outputs from the GMU JPSS Flood archive.
- `atlantis fetch` works for explicit bbox/date VIIRS extraction.
- `atlantis fetch-kurosiwo-viirs` works directly from the KuroSiwo catalogue or from a precomputed metadata CSV.
- `atlantis build-kurosiwo-metadata` derives the metadata CSV from the catalogue without using the notebook.
- `download_file()` is implemented and reuses existing files.
- KuroSiwo metadata can be converted into `FloodEvent` objects through `utils.kurosiwo`.

### Still planned / mostly stubbed

- GFM and RFM fetchers
- Harmoniser pipeline
- Raw and ML-ready archive writing
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
│  GFMFetcher   ──► STAC/EODC  │  planned              │
│  VIIRSFetcher ──► JPSS Flood │  implemented          │
│  RFMFetcher   ──► Phase C    │  planned              │
└──────────────┬───────────────┘                       │
               │ raw raster files (GeoTIFF, ZIP, …)    │
               ▼                                        │
┌──────────────────────────────┐                       │
│         HARMONISER           │                       │
│  Reprojector ──► Tiler ──►   │                       │
│  Normaliser                  │                       │
│                              │                       │
│  Target: EPSG:4326, 224×224  │                       │
│  tiles, values → [0, 1]      │                       │
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
├── cli.py                 # Typer CLI — fetch / fetch-kurosiwo-viirs / harmonise / archive / validate
├── config.py              # Pydantic settings hierarchy (env-var driven)
│
├── models/
│   ├── event.py           # FloodEvent dataclass
│   └── metadata.py        # TileMetadata / SourceMetadata (Pydantic)
│
├── fetchers/
│   ├── base.py            # AbstractFloodFetcher ABC + SearchResult / FetchResult
│   ├── registry.py        # @register_fetcher decorator + global registry
│   ├── gfm.py             # GFMFetcher — planned
│   ├── rfm.py             # RFMFetcher — planned / Phase C stub
│   └── viirs/
│       ├── __init__.py    # VIIRSFetcher — implemented
│       ├── backend.py     # Backend classes (NoaaS3Backend, GmuLegacyBackend)
│       ├── processor.py   # Raster processing (ViirsRasterProcessor)
│       └── data/
│           └── viirs_aois.geojson  # Packaged VIIRS AOI tile grid
│
├── harmoniser/
│   ├── reprojector.py     # Planned CRS reprojection + resampling
│   ├── tiler.py           # Planned uniform grid tiling
│   └── normaliser.py      # Planned value scaling + quality mask generation
│
├── archive/
│   ├── writer.py          # Planned raw / ml-ready Zarr writing + working checkpoints
│   └── reader.py          # Planned archive reader
│
├── validation/
│   ├── checker.py         # Planned archive checks
│   └── ml_loader.py       # Planned PyTorch smoke tests
│
└── utils/
    ├── geo.py            # BBox dataclass + validation / intersection helpers
    ├── io.py             # ensure_dir / get_cache_path / download_file
    └── kurosiwo.py       # Derive KuroSiwo metadata and build FloodEvent objects
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

`FloodEvent` is still the fundamental unit of work. It captures a region, a time window, and a set of sources. The current VIIRS path and the KuroSiwo helper both converge on this type.

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

For VIIRS, one `FetchResult` currently corresponds to one processed date, not one raw tile.

### 4.4 Dual-archive strategy

The intended long-term design still distinguishes:

| Archive     | Purpose                                         | Status  |
| ----------- | ----------------------------------------------- | ------- |
| `raw/`      | Preservation of fetched source data             | planned |
| `ml-ready/` | Harmonised, tiled, normalised training products | planned |

This strategy is still valid architecturally, but only the fetch layer is operational today.

### 4.5 Checkpoints

`ArchiveWriter.write_checkpoint()` and `is_checkpointed()` already exist and are accurate to the design. Checkpointing is one of the few implemented pieces in the archive layer.

---

## 5. Working VIIRS Flow

This section consolidates the operational material that used to live in the separate VIIRS document.

### 5.1 What You Need

- A checked out copy of this repository
- `uv` installed
- Network access to `https://jpssflood.gmu.edu/downloads/pub`
- Geo dependencies installed
- For KuroSiwo runs: either the catalogue at `assets/ks_catalogue.gpkg` or a precomputed metadata CSV

Install the required dependencies with:

```bash
uv sync --extra geo
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
        ├── Yangtze_2020_20200722_viirs_flood_extent.tif
        ├── Yangtze_2020_20200722_viirs_quality_mask.tif
        └── Yangtze_2020_20200722_viirs_permanent_water.tif
```

Use `--no-stream` to cache raw tiles to disk for reuse across runs. Use `--no-classify` to write raw integer pixel codes instead of the derived layers.

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
            ├── KuroSiwo_470_20201014_viirs_flood_extent.tif
            ├── KuroSiwo_470_20201014_viirs_quality_mask.tif
            └── KuroSiwo_470_20201014_viirs_permanent_water.tif
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

Current VIIRS processed outputs are:

- `*_flood_extent.tif` — binary flood mask derived from VIIRS values `>= 160`
- `*_quality_mask.tif` — `0` where cloud or water masks invalidate the pixel, `1` otherwise
- `*_permanent_water.tif` — binary mask derived from VIIRS permanent-water code `17`

These are suitable for analysis and for conversion through `to_dataset()`, but they are not yet wired into the harmoniser/archive stages.

### 5.7 Current Limitations

- Only the non-NRT JPSS archive is supported
- VIIRS currently writes GeoTIFF outputs, not Zarr
- GFM and RFM source paths are still stubs
- Harmonise, archive, and validate are not yet operational end to end
- The KuroSiwo metadata build step is now available in `src/`, and the default derived CSV path now lives under `data/metadata/` rather than notebook drafts.

---

## 6. Module Deep Dive

### 6.1 `config.py`

Configuration is still centered on `AtlantisConfig`, which combines:

- `HarmoniseConfig`
- `ArchiveConfig`
- `FetcherConfig`

Important current fetcher settings:

- `cache_dir` defaults to `~/.cache/atlantis`
- `timeout` defaults to 300 seconds
- `viirs_base_url` can override the JPSS archive base URL

### 6.2 `models/`

- `FloodEvent` is accurate and actively used
- `TileMetadata` is accurate and actively used by VIIRS
- `SourceMetadata` exists but is not yet central to the working flow

### 6.3 `fetchers/`

| Fetcher        | Data                            | Backend                | Status      |
| -------------- | ------------------------------- | ---------------------- | ----------- |
| `GFMFetcher`   | SAR flood maps                  | STAC/EODC              | stub        |
| `VIIRSFetcher` | Archived VIIRS flood composites | GMU JPSS Flood archive | implemented |
| `RFMFetcher`   | Modelled flood extent           | Phase C                | stub        |

`VIIRSFetcher` is no longer a placeholder. It is the current reference implementation for how a full fetcher should behave.

### 6.4 `harmoniser/`

The intended three-step design remains:

1. Reproject
2. Tile
3. Normalise and mask

But the main classes remain planned. The documentation should be read here as target architecture, not current capability.

### 6.5 `archive/`

The archive layout and dual raw / ml-ready concept remain the intended design. `ArchiveWriter` currently provides working checkpoint helpers, but raw and ML-ready Zarr writing are still not implemented.

### 6.6 `validation/`

Validation is still planned. The API surface exists, but the checks themselves remain placeholders.

### 6.7 `utils/`

- `geo.py` provides bbox validation and geometric helpers
- `io.py` provides directory creation, cache naming, and a working streaming downloader
- `kurosiwo.py` converts KuroSiwo metadata rows into `FloodEvent` objects for VIIRS extraction

---

## 7. CLI Commands

| Command                                                                              | Purpose                                                    | Status      |
| ------------------------------------------------------------------------------------ | ---------------------------------------------------------- | ----------- |
| `atlantis fetch --event E --source viirs --bbox ... --start-date ... --end-date ...` | Fetch VIIRS for an explicit bbox/date window               | implemented |
| `atlantis build-kurosiwo-metadata --catalogue ... --output ...`                      | Derive KuroSiwo metadata CSV from the GeoPackage           | implemented |
| `atlantis fetch-kurosiwo-viirs --catalogue ...`                                      | Fetch VIIRS for KuroSiwo cases directly from the catalogue | implemented |
| `atlantis fetch-kurosiwo-viirs --metadata ...`                                       | Fetch VIIRS for KuroSiwo cases from metadata CSV           | implemented |
| `atlantis harmonise --event E --source S`                                            | Reproject → tile → normalise                               | planned     |
| `atlantis archive --event E`                                                         | Write raw + ml-ready Zarr archives                         | planned     |
| `atlantis validate --event E`                                                        | Run archive and ML validation checks                       | planned     |
| `atlantis list-sources`                                                              | Print registered fetchers and descriptions                 | implemented |
| `atlantis list-events`                                                               | List events present in the archive                         | planned     |

---

## 8. Dependency Groups

```bash
uv sync                           # Core only
uv sync --extra geo               # xarray, zarr, rioxarray, odc-stac, pystac-client, pyproj, shapely, rasterio, requests, beautifulsoup4
uv sync --extra ml                # torch, numpy, scikit-learn, matplotlib
uv sync --extra notebooks         # geo + ml + notebook-only tooling such as earthkit-data, cartopy, metview
```

For the working VIIRS path, `uv sync --extra geo` is the relevant setup step.

---

## 9. Extending Atlantis

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

## 10. Design Principles

| Principle               | How Atlantis implements it                                                                       |
| ----------------------- | ------------------------------------------------------------------------------------------------ |
| Pluggable sources       | `@register_fetcher` decorator + global registry                                                  |
| Config-driven behavior  | Paths, timeouts, CRS, tile settings from config/env                                              |
| Separation of concerns  | Fetchers, harmoniser, archive, validation are distinct layers                                    |
| Type-safe core          | Dataclasses for events, Pydantic models for metadata                                             |
| Resumable processing    | Checkpoint markers in the archive layer                                                          |
| Evolving implementation | Current code can expose partial but useful workflows while preserving the long-term architecture |
