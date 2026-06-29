# GFM Python API

Python interface for the GFM fetcher. For CLI usage, see [overview.md](overview.md).
For the architectural breakdown, see [internals.md](internals.md).

## Basic usage

```python
from datetime import date
from pathlib import Path

from atlantis.fetchers.gfm import GFMFetcher
from atlantis.models.event import FloodEvent

event = FloodEvent(
    event_id="Valencia_2024",
    bbox=(-1.5, 38.8, 0.5, 40.0),
    start_date=date(2024, 10, 29),
    end_date=date(2024, 11, 4),
)

fetcher = GFMFetcher(strategy="aggregate")
results = fetcher.fetch(event, Path("data/Valencia_2024/gfm"))

ds = fetcher.to_dataset(results[0])
print(ds.data_vars)
print(float(ds["flood_fraction"].max()))
```

## Native / raw mode

Pass `classify=False` to emit the native SAR band codes without any derivation:

```python
fetcher = GFMFetcher(classify=False)
results = fetcher.fetch(event, Path("data/Valencia_2024/gfm"))

ds = fetcher.to_dataset(results[0])
print(list(ds.data_vars))  # ['ensemble_flood_extent', 'reference_water_mask']
```

## Search before fetch

```python
from datetime import date

from atlantis.fetchers.gfm import GFMFetcher
from atlantis.models.event import FloodEvent

event = FloodEvent(
    event_id="Valencia_2024",
    bbox=(-1.5, 38.8, 0.5, 40.0),
    start_date=date(2024, 10, 29),
    end_date=date(2024, 11, 4),
)

fetcher = GFMFetcher()
matches = fetcher.search(event)
print(len(matches), "STAC items")
print(matches[0].item_id, matches[0].timestamp)
```

Each search result corresponds to one STAC item. `fetch()` groups those items
by acquisition date before processing.

## Peak-window filtering and subsampling

```python
from atlantis.fetchers.gfm import GFMFetcher

fetcher = GFMFetcher(
    strategy="all",
    peak_days_before=2,
    peak_days_after=2,
    max_observations=3,
    peak_priority="balanced",
)
```

This keeps the peak inundation date plus nearby dates, using the same
windowing and subsampling logic documented in [pipeline.md](pipeline.md).

## Custom endpoint and reprojection settings

```python
from rasterio.enums import Resampling

from atlantis.fetchers.gfm import GFMFetcher

fetcher = GFMFetcher(
    api_url="https://stac.eodc.eu/api/v1",
    coarsen_factor=8,
    resampling=Resampling.nearest,
    keep_processed=False,
)
```

Use `coarsen_factor` to trade spatial detail for speed and speckle reduction.
`resampling` is applied during reprojection onto the canonical 1-arcmin grid.

## Harmonisation

```python
from atlantis.harmoniser import Harmoniser, write_harmonised_raster

harmoniser = Harmoniser()
ds_harm = harmoniser.harmonise(ds, source_id="gfm")
write_harmonised_raster(ds_harm["flood_fraction"], Path("harmonised/gfm_output.tif"))
```

The processor already snaps GFM results to the canonical global grid. The
harmoniser writes the usual uint8 percentage raster for downstream analysis.

## Dataset variables

`GFMFetcher.to_dataset()` returns an `xarray.Dataset` whose variables depend on
the `classify` flag.

**Classified mode** (`classify=True`, default):

| Variable          | Dtype     | Meaning                                                 |
| ----------------- | --------- | ------------------------------------------------------- |
| `flood_fraction`  | `float32` | Fraction of valid observations classified as flood      |
| `quality_mask`    | `uint8`   | `1` where at least one valid observation exists         |
| `permanent_water` | `uint8`   | `1` where permanent water exceeds 50% of valid coverage |

**Native / raw mode** (`classify=False`):

| Variable                | Dtype   | Meaning                                                                   |
| ----------------------- | ------- | ------------------------------------------------------------------------- |
| `ensemble_flood_extent` | `uint8` | Raw flood code: 0=dry, 1=flood, 255=nodata                                |
| `reference_water_mask`  | `uint8` | Raw water code: 0=land, 1=water (seasonal), 2=permanent water, 255=nodata |

## GFMFetcher parameters

| Parameter          | Type         | Default              | Description                                                                          |
| ------------------ | ------------ | -------------------- | ------------------------------------------------------------------------------------ | -------------------------- |
| `api_url`          | `str         | None`                | EODC STAC endpoint                                                                   | Override the STAC API root |
| `coarsen_factor`   | `int`        | `4`                  | Max-pool factor before reprojection (classified mode only)                           |
| `resampling`       | `Resampling` | `Resampling.average` | Reprojection resampling method (classified mode only)                                |
| `classify`         | `bool`       | `True`               | `True` = derive flood_fraction / quality / permanent_water; `False` = emit raw codes |
| `strategy`         | `str`        | `"peak"`             | One of `peak`, `aggregate`, or `all`                                                 |
| `keep_processed`   | `bool`       | `True`               | Write intermediate processed GeoTIFFs                                                |
| `peak_days_before` | `int`        | `0`                  | Window filter before the peak date                                                   |
| `peak_days_after`  | `int`        | `0`                  | Window filter after the peak date                                                    |
| `max_observations` | `int`        | `0`                  | Cap the number of returned dates after windowing                                     |
| `peak_priority`    | `str`        | `"post"`             | Subsampling bias: `post`, `pre`, or `balanced`                                       |

## Notes

- GFM has no download-versus-stream option like VIIRS or MODIS. It always
  loads Cloud-Optimised GeoTIFF assets via STAC discovery and `odc.stac`.
- For implementation details behind `search()`, `fetch()`, and `to_dataset()`, see
  [internals.md](internals.md) and the code in
  [src/atlantis/fetchers/gfm/**init**.py](../../src/atlantis/fetchers/gfm/__init__.py).
