# Atlantis — Architecture Guide

> **What is Atlantis?** An ML-ready archive pipeline for
> satellite-derived flood inundation observations. It fetches raw data
> from multiple EO sources, harmonises them to a common grid, and writes
> chunked Zarr archives immediately usable by PyTorch / scikit-learn.

---

## 1. Pipeline at a Glance

```
┌─────────────────────────────────────────────────────────────────┐
│                         FLOOD EVENT                             │
│   event_id · bbox · start_date · end_date · sources             │
└──────────────┬──────────────────────────────────────┬───────────┘
               │                                      │
               ▼                                      │
┌──────────────────────────────┐                       │
│           FETCHERS           │                       │
│  AbstractFloodFetcher +       │                       │
│  @register_fetcher registry  │                       │
│                              │                       │
│  GFMFetcher  ──► STAC/EODC   │                       │
│  VIIRSFetcher ──► NOAA CLASS │                       │
│  RFMFetcher  ──► (Phase C)   │                       │
└──────────────┬───────────────┘                       │
               │ raw raster files (GeoTIFF, HDF, …)     │
               ▼                                        │
┌──────────────────────────────┐                       │
│         HARMONISER           │                       │
│  Reprojector ──► Tiler ──►   │                       │
│  Normaliser                  │                       │
│                              │                       │
│  Target: EPSG:4326, 224×224  │                       │
│  tiles, values → [0, 1]     │                       │
└──────────────┬───────────────┘                       │
               │ harmonised xarray Datasets             │
               ▼                                        │
┌──────────────────────────────┐                       │
│           ARCHIVE            │                       │
│                              │                       │
│  ArchiveWriter               │                       │
│    └─► raw/  (Zarr, as-is)   │                       │
│    └─► ml-ready/ (Zarr,      │                       │
│           tiled & normalised) │                       │
│                              │                       │
│  ArchiveReader               │                       │
│    └─► read_raw()            │                       │
│    └─► read_ml_ready()       │                       │
│                              │                       │
│  Checkpoint markers (.done)  │                       │
│  for resumable pipelines     │                       │
└──────────────┬───────────────┘                       │
               │                                       │
               ▼                                       │
┌──────────────────────────────┐                       │
│          VALIDATION           │                       │
│                              │                       │
│  ArchiveChecker              │                       │
│    ├─► spatial alignment      │                       │
│    ├─► NaN patterns           │                       │
│    ├─► CRS consistency       │                       │
│    └─► value ranges           │                       │
│                              │                       │
│  MLLoaderValidator           │                       │
│    ├─► PyTorch Dataset smoke  │                       │
│    ├─► DataLoader batching   │                       │
│    └─► GPU transfer          │                       │
└──────────────────────────────────────────────────────┘
```

---

## 2. Package Layout

```
src/atlantis/
├── __init__.py            # Package entry point, version
├── cli.py                 # Typer CLI — fetch / harmonise / archive / validate
├── config.py              # Pydantic settings hierarchy (env-var driven)
│
├── models/                # Domain data structures
│   ├── event.py           # FloodEvent dataclass
│   └── metadata.py        # TileMetadata / SourceMetadata (Pydantic)
│
├── fetchers/              # Pluggable data-source adapters
│   ├── base.py            # AbstractFloodFetcher ABC + SearchResult /
│   │                      #   FetchResult dataclasses + FloodFetcher Protocol
│   ├── registry.py        # @register_fetcher decorator + global registry
│   ├── gfm.py             # GFMFetcher — STAC/EODC Sentinel-1/2/3
│   ├── viirs.py           # VIIRSFetcher — NOAA Day-Night Band
│   └── rfm.py            # RFMFetcher — Regional Flood Model (Phase C stub)
│
├── harmoniser/            # Raw → standard grid transformation
│   ├── reprojector.py     # CRS reprojection + resampling (rioxarray)
│   ├── tiler.py           # Uniform grid tiling (default 224×224 px)
│   └── normaliser.py      # Value scaling + quality mask generation
│
├── archive/               # Zarr storage layer
│   ├── writer.py          # ArchiveWriter — write raw / ml-ready Zarr
│   └── reader.py          # ArchiveReader — read Zarr back to xarray
│
├── validation/            # Integrity and ML smoke tests
│   ├── checker.py         # ArchiveChecker — spatial / NaN / CRS checks
│   └── ml_loader.py       # MLLoaderValidator — PyTorch smoke tests
│
└── utils/                 # Shared helpers
    ├── geo.py             # BBox dataclass + validate / intersects / tile_bbox
    └── io.py              # ensure_dir / get_cache_path / download_file
```

---

## 3. Core Abstractions

### 3.1 `FloodEvent` — the unit of work

```python
@dataclass
class FloodEvent:
    event_id: str                                    # e.g. "Valencia_2024"
    bbox: tuple[float, float, float, float]         # (west, south, east, north)
    start_date: date
    end_date: date
    sources: list[str]                              # ["gfm", "viirs"]
```

A `FloodEvent` fully describes **what to process**: where, when, and
from which satellites. It is the primary input to every pipeline stage.

### 3.2 `AbstractFloodFetcher` — pluggable source adapters

Sources are **not hardcoded** into the pipeline. New EO products are added by:

1. Subclassing `AbstractFloodFetcher`
2. Implementing `search()` → `list[SearchResult]` and `fetch()` → `list[FetchResult]`
3. Decorating with `@register_fetcher("source_name")`

The decorator registers the class in `fetcher_registry`, and the CLI / pipeline can enumerate or select sources at runtime without any conditional logic.

### 3.3 Dual-archive strategy

| Archive     | Purpose      | Contents                                                       |
| ----------- | ------------ | -------------------------------------------------------------- |
| `raw/`      | Preservation | Original data as-downloaded, with source CRS & resolution      |
| `ml-ready/` | Training     | Harmonised (reprojected, tiled, normalised) with quality masks |

The raw archive acts as a **lossless source of truth**; the ML-ready archive is a derived product that can be regenerated from raw if the harmonisation config changes.

### 3.4 Checkpoint system

`ArchiveWriter.write_checkpoint()` drops a `.done` marker file after each pipeline stage (fetch, harmonise, archive). `is_checkpointed()` lets the pipeline skip completed stages on re-run — enabling **resumable, failure-safe** processing of large flood events.

---

## 4. Module Deep-Dive

### 4.1 `config.py` — Settings hierarchy

All settings are Pydantic `BaseSettings` subclasses, read from environment variables prefixed with `ATLANTIS_` and optionally from a `.env` file:

```python
AtlantisConfig
├── harmonise: HarmoniseConfig
│   ├── target_crs: str          # default "EPSG:4326"
│   ├── target_resolution: float  # ~1 arc-second
│   ├── tile_size: int           # 224 (pixels, square)
│   ├── resampling: str          # "average" | "bilinear" | "nearest" | "cubic"
│   └── normalise_range: tuple   # (0.0, 1.0)
│
├── archive: ArchiveConfig
│   ├── archive_root: Path        # default ~/atlantis-data
│   ├── raw_subdir: str          # "raw"
│   ├── ml_subdir: str           # "ml-ready"
│   ├── checkpoint_dir: str      # ".checkpoints"
│   └── default_chunk_size: int  # 224
│
└── fetcher: FetcherConfig
    ├── cache_dir: Path           # default ~/.cache/atlantis
    ├── timeout: int             # 300 s
    ├── max_retries: int         # 3
    ├── gfm_api_url: str | None
    └── viirs_base_url: str | None
```

### 4.2 `models/` — Domain types

- **`FloodEvent`** — Pure dataclass, validated in `__post_init__` (lon/lat bounds, west≤east, south≤north, end_date≥start_date).
- **`TileMetadata`** — Pydantic model for per-tile provenance: CRS, resolution, bbox, cloud fraction, snow flag, quality bitmask, permanent-water availability.
- **`SourceMetadata`** — Pydantic model describing a data source's license, temporal/spatial coverage.

### 4.3 `fetchers/` — Source adapters

| Fetcher        | Data                           | Interface               | Status         |
| -------------- | ------------------------------ | ----------------------- | -------------- |
| `GFMFetcher`   | Sentinel-1/2/3 SAR flood maps  | STAC API (EODC)         | Stub           |
| `VIIRSFetcher` | Day-Night Band flood detection | NOAA CLASS web scraping | Stub           |
| `RFMFetcher`   | Modelled flood extent          | TBD (EFAS/GloFAS)       | Stub / Phase C |

All three share the same interface contract (`AbstractFloodFetcher`), making it trivial to swap or extend them.

### 4.4 `harmoniser/` — Three-step standardisation

```
Raw xarray.Dataset
       │
       ▼  1. Reprojector.reproject()
   Target CRS + resolution (default EPSG:4326, ~1″)
       │
       ▼  2. Tiler.tile_dataset()
   List[(tile_dataset, tile_metadata)]
       │
       ▼  3. Normaliser.normalise() + generate_quality_mask()
   Harmonised Dataset — values ∈ [0, 1], NaN → quality flags
```

- **`Reprojector`** — Uses `rioxarray` for CRS reprojection and resampling.
- **`Tiler`** — Chunks the grid into square tiles (default 224×224, matching common CNN input sizes). Supports overlap.
- **`Normaliser`** — Scales values to `[0, 1]`; generates a `uint8` quality mask with bit flags for fill, cloud, snow, and out-of-AOI pixels.

### 4.5 `archive/` — Zarr persistence

`ArchiveWriter` organises data as:

```
{archive_root}/
├── raw/
│   └── {event_id}/
│       └── {source_id}.zarr/
├── ml-ready/
│   └── {event_id}/
│       └── {source_id}.zarr/
└── .checkpoints/
    └── {event_id}/
        ├── {source_id}_fetch.done
        ├── {source_id}_harmonise.done
        └── {source_id}_archive.done
```

`ArchiveReader` exposes `read_raw()`, `read_ml_ready()`, `list_events()`, and `list_sources()` as the complement to the writer.

### 4.6 `validation/` — Quality gates

- **`ArchiveChecker`** runs **four checks** (all placeholder implementations currently):
  - Spatial alignment across variables
  - NaN / missing-data fraction
  - CRS consistency
  - Value range validation
- **`MLLoaderValidator`** runs **three PyTorch smoke tests**:
  - `Dataset.__len__` / `__getitem__`
  - `DataLoader` batching
  - GPU tensor transfer (if CUDA available)

Both are intended to run as CI gates or pre-training sanity checks.

### 4.7 `utils/` — Shared helpers

- **`geo.py`** — `BBox` dataclass, `validate_bbox()`, `bbox_intersects()`, `bbox_area()`, `tile_bbox()`.
- **`io.py`** — `ensure_dir()`, `get_cache_path()` (MD5-hashed URL filenames), `download_file()` with ETag-based cache validation.

---

## 5. CLI Commands

| Command                                   | Purpose                                    |
| ----------------------------------------- | ------------------------------------------ |
| `atlantis fetch --event E --source S`     | Download raw raster data for a flood event |
| `atlantis harmonise --event E --source S` | Reproject → tile → normalise               |
| `atlantis archive --event E`              | Write raw + ml-ready Zarr archives         |
| `atlantis validate --event E`             | Run spatial and ML validation checks       |
| `atlantis list-sources`                   | Print registered fetchers and descriptions |
| `atlantis list-events`                    | List events present in the archive         |

---

## 6. Dependency Groups

```bash
uv sync                           # Core only
uv sync --extra geo               # xarray, zarr, rioxarray, STAC client, pyproj, shapely
uv sync --extra ml                # torch, numpy, scikit-learn, matplotlib
uv sync --extra notebooks         # geo + ml + rasterio, cartopy, geopandas, odc-stac, etc.
```

---

## 7. Extending Atlantis — Adding a New Fetcher

Suppose you want to add a fetcher for a new EO product, e.g. **Sentinel-2 L2A** via Copernicus Data Space:

**Step 1 — Create the fetcher class**

```python
# src/atlantis/fetchers/sentinel2.py
from pathlib import Path
from atlantis.fetchers.base import AbstractFloodFetcher, FetchResult, SearchResult
from atlantis.fetchers.registry import register_fetcher
from atlantis.models.event import FloodEvent

@register_fetcher("sentinel2")
class Sentinel2Fetcher(AbstractFloodFetcher):
    source_id: str = "sentinel2"

    def __init__(self, api_url: str | None = None) -> None:
        self.api_url = api_url or "https://catalogue.dataspace.copernicus.eu/..."

    def search(self, event: FloodEvent) -> list[SearchResult]:
        # TODO: implement STAC query within event.bbox and date range
        ...

    def fetch(self, event: FloodEvent, output_dir: Path) -> list[FetchResult]:
        # TODO: download files, return FetchResult objects
        ...

    def to_dataset(self, result: FetchResult) -> "xr.Dataset":
        # TODO: convert to standard xarray with flood_extent variable
        ...
```

**Step 2 — Import in `cli.py`**

```python
from atlantis.fetchers import ..., sentinel2  # noqa: F401
```

**Step 3 — Use it**

```bash
atlantis fetch --event Valencia_2024 --source sentinel2
```

No changes to `cli.py` logic, `harmoniser`, `archive`, or `validation` are needed — the registry pattern keeps the pipeline fully decoupled from individual sources.

---

## 8. Design Principles

| Principle                | How Atlantis implements it                                                        |
| ------------------------ | --------------------------------------------------------------------------------- |
| **Pluggable sources**    | `@register_fetcher` decorator + global registry dict                              |
| **Lossless raw archive** | Raw Zarr never modified; ML-ready always derived                                  |
| **Resumable pipelines**  | `.done` checkpoint files per stage                                                |
| **Config-driven**        | All paths, CRS, tile sizes from env vars / `.env`                                 |
| **ML-first output**      | Tiled, chunked Zarr with quality masks — zero preprocessing                       |
| **Type-safe core**       | Pydantic models for metadata, dataclasses for events, Protocol/ABC for interfaces |
