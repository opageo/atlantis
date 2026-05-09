# Plan: Atlantis Repository Architecture & Base Classes

## TL;DR

Design the repo structure and base abstractions for the Atlantis flood inundation pipeline: fetchers (GFM, VIIRS, etc.), harmoniser, archive writer, validator, and CLI orchestration. The architecture follows the proposal's four-component design (Fetchers → Harmoniser → Writer → Orchestrator) with a plugin-like fetcher pattern.

---

## Phase 1: Repository Structure

Proposed layout under `src/atlantis/`:

```
src/atlantis/
├── __init__.py
├── cli.py                    # Typer CLI (fetch, harmonise, archive, validate)
├── config.py                 # Pydantic settings, paths, CRS defaults
├── models/
│   ├── __init__.py
│   ├── event.py              # FloodEvent dataclass (id, bbox, dates, sources)
│   └── metadata.py           # TileMetadata, SourceMetadata schemas
├── fetchers/
│   ├── __init__.py
│   ├── base.py               # AbstractFloodFetcher ABC
│   ├── gfm.py                # GFMFetcher (STAC/EODC)
│   ├── viirs.py              # VIIRSFetcher (web scraping)
│   ├── rfm.py                # RFMFetcher (stub, Phase C)
│   └── registry.py           # Fetcher registry/discovery
├── harmoniser/
│   ├── __init__.py
│   ├── reprojector.py        # CRS reprojection, resampling
│   ├── tiler.py              # Tile gridding (224×224 chunks)
│   └── normaliser.py         # Value normalisation, mask generation
├── archive/
│   ├── __init__.py
│   ├── writer.py             # Zarr writer (raw + ML-ready)
│   └── reader.py             # Zarr/STAC reader for validation
├── validation/
│   ├── __init__.py
│   ├── checker.py            # Spatial consistency, NaN checks
│   └── ml_loader.py          # PyTorch Dataset/DataLoader smoke test
└── utils/
    ├── __init__.py
    ├── geo.py                # Shared geo helpers (bbox, tiling)
    └── io.py                 # Download, caching, checkpointing
```

Key decisions:

- `models/` holds pure data classes (no I/O), used everywhere
- `fetchers/` uses a registry pattern so new sources are plug-and-play
- `harmoniser/` is source-agnostic — works on xarray Datasets
- `archive/` handles both raw and ML-ready Zarr stores

---

## Phase 2: Base Classes

### 1. `FloodEvent` (models/event.py)

Dataclass representing a flood event:

- `event_id: str` (e.g. "Valencia", matches Kuro Siwo IDs)
- `bbox: tuple[float, float, float, float]` (west, south, east, north)
- `start_date: datetime.date`
- `end_date: datetime.date`
- `sources: list[str]` (e.g. ["gfm", "viirs"])

### 2. `TileMetadata` (models/metadata.py)

Pydantic model for per-tile metadata:

- `event_id, source_id, fetch_timestamp`
- `crs, resolution, bbox`
- `cloud_fraction, snow_flag, quality_bitmask`
- `permanent_water_mask_available: bool`

### 3. `AbstractFloodFetcher` (fetchers/base.py)

ABC defining the fetcher contract:

- `source_id: str` (class attribute)
- `search(event: FloodEvent) -> list[SearchResult]` — query catalog/API
- `fetch(event: FloodEvent, output_dir: Path) -> list[FetchResult]` — download raw data
- `to_dataset(result: FetchResult) -> xr.Dataset` — load into xarray with standard var names
- Common var names: `flood_extent` (float32, 0-1), `quality_mask` (uint8), `permanent_water` (uint8)

### 4. `FetchResult` (fetchers/base.py)

Dataclass returned by fetchers:

- `event_id, source_id`
- `files: list[Path]` (downloaded TIFFs)
- `metadata: TileMetadata`
- `timestamp: datetime`

### 5. `ArchiveWriter` (archive/writer.py)

- `write_raw(dataset: xr.Dataset, event: FloodEvent, source_id: str)` — append to raw Zarr
- `write_ml_ready(dataset: xr.Dataset, event: FloodEvent, source_id: str, config: HarmoniseConfig)` — append to ML Zarr

### 6. `HarmoniseConfig` (config.py)

Pydantic model for harmonisation parameters:

- `target_crs: str = "EPSG:4326"`
- `target_resolution: float` (degrees)
- `tile_size: int = 224`
- `resampling: str = "average"`
- `normalise_range: tuple[float, float] = (0.0, 1.0)`

### 7. Fetcher Registry (fetchers/registry.py)

Simple dict-based registry:

- `register(name: str, fetcher_cls: type[AbstractFloodFetcher])`
- `get(name: str) -> type[AbstractFloodFetcher]`
- Auto-discovery via decorator `@register_fetcher("gfm")`

---

## Phase 3: CLI Wiring

Update `cli.py` to accept:

- `atlantis fetch --event <id> --source <gfm|viirs|all>`
- `atlantis harmonise --event <id> --config <path>`
- `atlantis archive --event <id>` (writes both raw + ML-ready)
- `atlantis validate --archive <path>`

Add `harmonise` command (proposal says harmonise is separate from archive write).

---

## Relevant files

- `src/atlantis/cli.py` — expand CLI commands with event/source args
- `pyproject.toml` — already has correct deps; may need `pydantic` added
- Notebooks — reference implementations for GFM fetcher (`Extract_GFM_Inundation.ipynb`), VIIRS fetcher (`Extract_VIIRS_inundation.ipynb`), harmonisation/benchmarking logic (`Bench_CMF_GFM_Inundation.ipynb`)

## Verification

1. All new modules importable: `python -c "from atlantis.fetchers.base import AbstractFloodFetcher"`
2. `pytest` passes with existing + new unit tests
3. `ruff check src/` clean
4. Stub fetchers can be instantiated and called (return empty results)
5. `atlantis --help` shows all subcommands

## Decisions

- Use Pydantic for config/metadata models (type safety, serialisation)
- Fetcher registry pattern over entry-points (simpler for now, can migrate later)
- Standard variable names across all sources: `flood_extent`, `quality_mask`, `permanent_water`
- Phase A/B sources (GFM, VIIRS) get full implementations; others get stubs
- Keep notebooks as-is for reference; they are not part of the package

## Further Considerations

1. **Event catalog format**: Should flood events be defined in a YAML/JSON file or fetched from Kuro Siwo programmatically? _Recommend_: YAML file in `data/events.yaml` for simplicity, with option to load from Kuro Siwo later.
2. **Checkpointing strategy**: File-based (write a `.done` marker per event/source) vs. SQLite. _Recommend_: file-based markers for simplicity.
3. **Should `pydantic` be added to core deps?** _Recommend_: Yes, for config and metadata validation.
