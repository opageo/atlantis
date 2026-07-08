# VIIRS Flood Detection Fetcher

> Suomi-NPP / NOAA-20 flood detection at 375 m resolution from the
> JPSS Flood archive (NOAA S3 or GMU Legacy backend).

## Module overview

| File           | Purpose                                                             |
| -------------- | ------------------------------------------------------------------- |
| `__init__.py`  | `VIIRSFetcher` — AOI grid loading, search, fetch, strategy dispatch |
| `backend.py`   | Strategy abstraction for VIIRS data sources (NoaaS3, GmuLegacy)     |
| `processor.py` | Raster pipeline: mosaic → clip → classify → write GeoTIFF           |
| `selection.py` | Peak-flood date selection from multi-date fetches                   |
| `dataset.py`   | `ProcessedTile` → xarray Dataset conversion                         |

## Data assets

| File                      | Purpose                                                      | Packaging                                           |
| ------------------------- | ------------------------------------------------------------ | --------------------------------------------------- |
| `data/viirs_aois.geojson` | Global 15° × 15° land-intersecting AOI tile grid (136 tiles) | Bundled in wheel via `pyproject.toml` hatch include |

The AOI grid lives **alongside the fetcher code**, not in the repo-level
`assets/` directory. This is intentional:

- It is a **reference grid**, not user data — the fetcher cannot function without it.
- Packaging it with the module ensures it is available after `pip install`
  without requiring `git checkout` or LFS.
- `assets/` is reserved for **LFS user data** (KuroSiwo catalogue, etc.)
  that is pulled separately.
- In development, `uv run atlantis setup` ensures it is present via git restore.

## Processing pipeline

```
Event bbox + dates
  │
  ▼
Load AOI grid (viirs_aois.geojson)
  │
  ▼
Intersect bbox → find matching AOI tiles
  │
  ▼
For each date:
  │  Query backend for remote filenames
  │  Download ZIPs (or stream via /vsicurl/)
  │  Extract TIFFs
  ▼
Mosaic tiles → clip to bbox
  │
  ▼
Classify: water_fraction, flood_fraction, reference_water, exclusion_mask
  │
  ▼
Write GeoTIFFs (or return in-memory for peak strategy)
```
