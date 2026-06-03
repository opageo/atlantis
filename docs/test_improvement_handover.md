# Test Improvement Handover Document

## Overview

This document provides context about ongoing test improvements for the Atlantis project, intended for another agent (AI or human) to continue the work seamlessly.

---

## Current State (as of 30 May 2026)

### What was accomplished in the last session

1. **`tests/test_config.py`** — New file. Tests for `atlantis.config` covering default config, environment variable overrides, `.env` file loading, validation of `ATLANTIS_VIIRS_BACKEND`, and custom resolution configurations.

2. **`tests/fetchers/test_backend.py`** — New file. Tests for `ViirsBackend` abstract class (ABC enforcement, protocol satisfaction), `NoaaS3Backend` (URL building, directory listing, filename matching), and `GmuLegacyBackend` (URL building, filename patterns).

3. **`tests/fetchers/test_processor.py`** — New file. Tests for `ViirsRasterProcessor` using synthetic GeoTIFF fixtures: mosaic-and-clip pipeline, classification (flood extent, quality mask, permanent water), flood threshold parameter, and the single-tile shortcut.

4. **`tests/utils/test_io.py`** — New file. Tests for `ensure_dir`, `get_cache_path`, ETag get/set, and download_file structure.

5. **`tests/fetchers/test_fetcher_base.py`** — New file. Tests for `SearchResult`/`FetchResult` dataclasses, `FloodFetcher` protocol runtime checks (complete/incomplete/missing attributes), and `AbstractFloodFetcher` ABC (instantiation, abstract methods, protocol conformance, `to_dataset` default).

6. **`tests/models/test_metadata.py`** — New file. Tests for `TileMetadata` (minimal/full construction, `cloud_fraction` validation boundaries) and `SourceMetadata`.

7. **`tests/models/test_event.py`** — Expanded. Added tests for: single-day event, dateline-crossing bbox, global bbox, default_factory isolation, and `__repr__`.

### Files previously existing (not modified)

- `tests/fetchers/test_viirs.py` — Existing VIIRSFetcher integration tests
- `tests/fetchers/test_registry.py` — Fetcher registry tests
- `tests/harmoniser/test_harmoniser.py` — Harmoniser integration tests
- `tests/harmoniser/test_normaliser.py` — Normaliser tests
- `tests/harmoniser/test_reprojector.py` — Reprojector tests
- `tests/utils/test_geo.py` — Geo utility tests
- `tests/utils/test_kurosiwo.py` — KuroSiwo utility tests
- `tests/test_cli.py` — CLI command tests
- `tests/archive/` — Archive tests (empty `__init__.py` only)
- `tests/validation/` — Validation tests (empty `__init__.py` only)

---

## Remaining Work — Priority Order

### 1. Expand `test_viirs.py` — VIIRSFetcher class (HIGH)

The VIIRSFetcher `__init__.py` in `src/atlantis/fetchers/viirs/` contains the main orchestrator. Current tests cover basic flow. Add tests for:

- [ ] `VIIRSFetcher.__init__` — constructor defaults (classify, stream, backend, flood_min_code)
- [ ] `VIIRSFetcher.search()` — with mock backend returning results
- [ ] `VIIRSFetcher.fetch()` — end-to-end mock test (search → download → process)
- [ ] `VIIRSFetcher.fetch()` with `stream=True` (no download called)
- [ ] `VIIRSFetcher.fetch()` with no search results (returns `[]`)
- [ ] `VIIRSFetcher.to_dataset()` — convert FetchResult to xarray Dataset
- [ ] Flood threshold propagation via `flood_min_code`
- [ ] Backend selection via `ATLANTIS_VIIRS_BACKEND` env var (use monkeypatch)

### 2. Expand `test_harmoniser.py` — Harmoniser and sub-modules (HIGH)

- [ ] Harmoniser edge cases: empty input, single raster, non-overlapping rasters
- [ ] Harmoniser with `--dry-run` flag
- [ ] Reprojector: target CRS different from source, target resolution scaling
- [ ] Reprojector: `average` vs `mode` resampling methods
- [ ] Normaliser: float32 output range [0, 1], nodata handling
- [ ] Tiler: tile splitting, overlap handling

### 3. Create `test_gfm.py` — GFM Fetcher (HIGH)

Read `src/atlantis/fetchers/gfm.py` and create tests:

- [ ] GFM fetcher structure (implements AbstractFloodFetcher?)
- [ ] Any GFM-specific logic or helpers
- [ ] Mock any external API calls

### 4. Create `test_rfm.py` — RFM Fetcher (MEDIUM)

Read `src/atlantis/fetchers/rfm.py` and create tests similarly to GFM.

### 5. Create `test_plot.py` — Plotting utilities (MEDIUM)

Read `src/atlantis/utils/plot.py` and create tests:

- [ ] Plot generation functions
- [ ] Colormap handling
- [ ] Figure save/display

### 6. Create `test_archive.py` — Archive reader/writer (LOW)

Read `src/atlantis/archive/reader.py` and `src/atlantis/archive/writer.py`:

- [ ] Zarr archive writing
- [ ] Zarr archive reading
- [ ] Metadata round-trip

### 7. Create `test_validation.py` — Validation checker/loader (LOW)

Read `src/atlantis/validation/checker.py` and `src/atlantis/validation/ml_loader.py`.

### 8. Add coverage configuration to `pyproject.toml` (LOW)

```toml
[tool.coverage.run]
source = ["atlantis"]
omit = ["*/tests/*", "*/scripts/*"]
```

And optionally add a `pytest-cov` session to the Makefile.

---

## Key Source Files to Read

| Module             | File                                      | Purpose                   |
| ------------------ | ----------------------------------------- | ------------------------- |
| VIIRSFetcher       | `src/atlantis/fetchers/viirs/__init__.py` | Main fetcher orchestrator |
| GFM Fetcher        | `src/atlantis/fetchers/gfm.py`            | GFM data fetcher          |
| RFM Fetcher        | `src/atlantis/fetchers/rfm.py`            | RFM data fetcher          |
| Fetcher Registry   | `src/atlantis/fetchers/registry.py`       | Global fetcher registry   |
| Harmoniser         | `src/atlantis/harmoniser/__init__.py`     | Harmonisation pipeline    |
| Normaliser         | `src/atlantis/harmoniser/normaliser.py`   | Data normalisation        |
| Reprojector        | `src/atlantis/harmoniser/reprojector.py`  | CRS reprojection          |
| Tiler              | `src/atlantis/harmoniser/tiler.py`        | Tiling logic              |
| Plot utils         | `src/atlantis/utils/plot.py`              | Plotting utilities        |
| KuroSiwo utils     | `src/atlantis/utils/kurosiwo.py`          | KuroSiwo event building   |
| Archive reader     | `src/atlantis/archive/reader.py`          | Zarr archive reading      |
| Archive writer     | `src/atlantis/archive/writer.py`          | Zarr archive writing      |
| Validation checker | `src/atlantis/validation/checker.py`      | Archive validation        |
| ML Loader          | `src/atlantis/validation/ml_loader.py`    | ML data loading           |
| CLI                | `src/atlantis/cli.py`                     | CLI entry points          |

---

## Existing Test Infrastructure

- **Pytest** with `pytest-cov` for coverage
- **tmp_path** fixture for temporary directory tests
- **monkeypatch** for env variable mocking
- Synthetic GeoTIFF creation in `test_processor.py` (using `rasterio` + `numpy`)
- Existing `conftest.py` patterns to follow

### Test patterns used

```python
# For module-level functions (pure logic):
class TestFunctionName:
    def test_normal_case(self): ...
    def test_edge_case(self): ...

# For classes:
class TestClassName:
    def test_construction(self): ...
    def test_method_name(self): ...

# For protocol/ABC tests:
def test_protocol_check(): ...
def test_abstract_enforcement(): ...
```

---

## Coverage Goals

| Area                          | Current (est.) | Target |
| ----------------------------- | -------------- | ------ |
| `utils/geo.py`                | ~80%           | ~90%   |
| `utils/io.py`                 | ~60%           | ~90%   |
| `models/`                     | ~70%           | ~90%   |
| `fetchers/base.py`            | ~85%           | ~90%   |
| `fetchers/viirs/backend.py`   | ~80%           | ~90%   |
| `fetchers/viirs/processor.py` | ~70%           | ~90%   |
| `fetchers/viirs/__init__.py`  | ~60%           | ~85%   |
| `fetchers/gfm.py`             | 0%             | ~80%   |
| `fetchers/rfm.py`             | 0%             | ~80%   |
| `fetchers/registry.py`        | ~80%           | ~90%   |
| `harmoniser/`                 | ~60%           | ~85%   |
| `utils/plot.py`               | 0%             | ~80%   |
| `utils/kurosiwo.py`           | ~50%           | ~85%   |
| `archive/`                    | 0%             | ~70%   |
| `validation/`                 | 0%             | ~70%   |
| `cli.py`                      | ~50%           | ~80%   |
| `config.py`                   | ~80%           | ~90%   |

**Overall target: ≥80% line coverage**

---

## Running Tests

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=atlantis --cov-report=term-missing

# Run specific file
uv run pytest tests/fetchers/test_backend.py -v

# Run specific test class
uv run pytest tests/fetchers/test_backend.py::TestNoaaS3Backend -v
```

---

## Architecture Notes for Test Writers

1. **VIIRSFetcher** uses a **backend pattern** — `NoaaS3Backend` or `GmuLegacyBackend` handle URL listing, while `ViirsRasterProcessor` handles raster operations. Mock the backend in fetcher tests.

2. **AOI grid** (`viirs_aois.geojson`) defines tile boundaries. Tests that need tile intersection should work with a simplified in-memory grid.

3. **Classification** uses numpy boolean indexing — test with small synthetic arrays.

4. **Harmoniser** pipeline: reproject → normalise → quality mask. Each step is a separate module.

5. **FloodEvent** is a plain dataclass with `__post_init__` validation — no external dependencies.
