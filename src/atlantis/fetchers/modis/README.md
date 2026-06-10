# `modis/` — MODIS MCDWD flood fetcher

Sensor-specific implementation of the
[`AbstractFloodFetcher`](../base.py) protocol for the NASA MODIS MCDWD
(MODIS Composite Daily Water Detection) product family. See
[`docs/modis/overview.md`](../../../../docs/modis/overview.md) for the user-facing
reference.

## Module map

```
modis/
├─ __init__.py    # MODISFetcher class, registered as @register_fetcher("modis")
├─ backend.py     # LanceGeotiffBackend (streamable) + LaadsHdf4Backend (download)
├─ processor.py   # Tile-grid helpers + ModisRasterProcessor (mosaic / clip / classify)
├─ selection.py   # flood_pixel_count + cloud_aware_score peak selectors
├─ dataset.py     # ProcessedTile → xarray.Dataset
└─ README.md      # this file
```

## Pipeline (one date)

```
┌─────────────────────────────────────────────────────────────────────┐
│ MODISFetcher.fetch()                                                │
│   ├─ search()      → tile h/v derivation + backend listing          │
│   ├─ download or pass remote URLs                                   │
│   │   • LANCE GeoTIFFs: stream via /vsicurl/ + GDAL_HTTP_HEADERS    │
│   │   • LAADS HDF4: download then open the requested subdataset    │
│   ├─ ModisRasterProcessor.process_tiles                             │
│   │   ├─ mosaic (rasterio.merge)                                    │
│   │   ├─ clip (rasterio.mask, crop=True)                            │
│   │   └─ classify pixels (0/1/2/3/255 → flood/recurring/permanent)  │
│   └─ strategy dispatch (peak / aggregate / all)                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Output layers (with `--classify`)

| Variable          | Source class | Encoding on disk                  |
| ----------------- | ------------ | --------------------------------- |
| `flood_fraction`  | class 3      | uint8 percent (0–100), nodata=255 |
| `recurring_flood` | class 2      | uint8 (0/1), nodata=0             |
| `permanent_water` | class 1      | uint8 (0/1), nodata=0             |
| `quality_mask`    | class != 255 | uint8 (0/1), nodata=0             |

`recurring_flood` is MODIS-specific and has no VIIRS counterpart.

## Auth

Both backends require an Earthdata bearer token. Set
`EARTHDATA_TOKEN` in the environment before running. The streaming
path injects the token into GDAL via `rasterio.Env(GDAL_HTTP_HEADERS=...)`
inside `MODISFetcher.fetch()`; the download path forwards it to
`requests` via `download_file(headers=...)`.
