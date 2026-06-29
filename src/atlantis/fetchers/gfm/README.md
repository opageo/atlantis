# `gfm/` — GFM flood fetcher

Sensor-specific implementation of the
[`AbstractFloodFetcher`](../base.py) protocol for the
[Global Flood Monitor (GFM)](https://www.globalfloodmonitor.org/) product, which
provides near-real-time Sentinel-1 SAR-based inundation maps. See
[`docs/gfm/overview.md`](../../../../docs/gfm/overview.md) for the user-facing
reference.

## Module map

```
gfm/
├─ __init__.py    # GFMFetcher class, registered as @register_fetcher("gfm")
│                 # GfmSearchDiagnostics — structured empty-fetch diagnostics
├─ backend.py     # GfmStacBackend — EODC STAC discovery + item grouping
├─ processor.py   # GfmRasterProcessor — coarsen → reproject → classify
├─ selection.py   # flood_pixel_count + peak selectors
├─ dataset.py     # GfmProcessedTile → xarray.Dataset
└─ README.md      # this file
```

## Pipeline (one date)

```
┌─────────────────────────────────────────────────────────────────────┐
│ GFMFetcher.fetch()                                                  │
│   ├─ search() / GfmStacBackend.search()                             │
│   │     EODC STAC API → ItemCollection (pystac_client)              │
│   ├─ group_items_by_date() → {YYYYMMDD: [items]}                    │
│   ├─ GfmRasterProcessor.process_items()                             │
│   │   ├─ odc.stac.load() — native CRS, full resolution              │
│   │   ├─ coarsen (max-pool, factor = coarsen_factor)                │
│   │   ├─ reproject → EPSG:4326                                      │
│   │   └─ classify: accumulate pixel counts → flood_fraction, etc.   │
│   │      (skipped when --no-classify; native bands emitted as-is)   │
│   └─ strategy dispatch (peak / aggregate / all)                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Output layers

### With `--classify` (default)

| Variable          | Source                       | Encoding on disk |
| ----------------- | ---------------------------- | ---------------- |
| `flood_fraction`  | flood pixel accumulator      | float32 (0–1)    |
| `quality_mask`    | valid observations count > 0 | uint8 (0/1)      |
| `permanent_water` | reference water mask band    | uint8 (0/1)      |

### With `--no-classify` (native/raw mode)

| Variable                | Source                         |
| ----------------------- | ------------------------------ |
| `ensemble_flood_extent` | GFM ensemble band, reprojected |
| `reference_water_mask`  | GFM reference water band       |

## Backend

`GfmStacBackend` wraps `pystac_client` to query the
[EODC STAC API](https://stac.eodc.eu/api/v1). No authentication is required.
The collection ID is `GFM`.

Unlike VIIRS and MODIS, GFM has a **single backend** — there is no alternate
download source. Data is always streamed from Cloud-Optimised GeoTIFFs.

## Diagnostics

`GFMFetcher.search()` populates `fetcher.last_diagnostics`
(`GfmSearchDiagnostics`) after every call. Useful fields:

| Field/property        | Meaning                                          |
| --------------------- | ------------------------------------------------ |
| `items_found`         | Number of STAC items returned by the search      |
| `dates_found`         | Number of unique acquisition dates               |
| `network_failure`     | True if the STAC request raised an exception     |
| `last_network_error`  | String of the last caught exception              |
| `network_unreachable` | Property alias for `network_failure`             |
| `no_items_found`      | True when search succeeded but returned no items |

The CLI's `_report_empty_gfm_fetch()` reads these fields to emit
actionable hints when `atlantis fetch --source gfm …` returns nothing.
