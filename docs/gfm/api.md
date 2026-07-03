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

This example opts into `strategy="aggregate"` explicitly. `GFMFetcher()`
itself defaults to `strategy="peak"`, matching the CLI.

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
`resampling` is applied during reprojection onto the ~80 m processed grid.

## Harmonisation

```python
from atlantis.harmoniser import Harmoniser, write_harmonised_raster

harmoniser = Harmoniser()
ds_harm = harmoniser.harmonise(ds, source_id="gfm")
write_harmonised_raster(ds_harm["flood_fraction"], Path("harmonised/gfm_output.tif"))
```

The processor already snaps fetched GFM results to the canonical global grid.
At the default settings, harmonisation mainly re-encodes `flood_fraction` into
the usual uint8 percentage raster for downstream analysis. The written
harmonised GeoTIFF contains the flood layer only; `quality_mask` and
`permanent_water` remain available in the in-memory dataset.

## Dataset variables

`GFMFetcher.to_dataset()` returns an `xarray.Dataset` whose variables depend on
the `classify` flag. `flood_fraction` is a **derived** layer (built from
observation counts, not a raw code); the native bands are passed through
untouched. See the full catalogue in [the layer reference](../layers.md) or via
`atlantis list-layers --source gfm`.

**Derived layers** (`classify=True`, default):

| Variable          | Dtype     | Meaning                                                                           |
| ----------------- | --------- | --------------------------------------------------------------------------------- |
| `flood_fraction`  | `float32` | Fraction of valid observed coverage classified as flood                           |
| `quality_mask`    | `uint8`   | Valid-observation coverage mask: `1` where any valid observation exists           |
| `permanent_water` | `uint8`   | Derived mask: `1` where `reference_water_mask == 2` exceeds 50% of valid coverage |

**Native layers** (`classify=False`):

| Variable                | Dtype   | Meaning                                                                   |
| ----------------------- | ------- | ------------------------------------------------------------------------- |
| `ensemble_flood_extent` | `uint8` | Raw flood code: 0=dry, 1=flood, 255=nodata                                |
| `reference_water_mask`  | `uint8` | Raw water code: 0=land, 1=water (seasonal), 2=permanent water, 255=nodata |

## GFMFetcher parameters

| Parameter          | Type            | Default              | Description                                                                                                                                                          |
| ------------------ | --------------- | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `api_url`          | `Optional[str]` | `None`               | Override the default EODC STAC endpoint                                                                                                                              |
| `coarsen_factor`   | `int`           | `4`                  | Mean-pool factor for the class masks (classified); also sets the processed grid spacing (~20 m × factor) in both modes                                               |
| `resampling`       | `Resampling`    | `Resampling.average` | Reprojection resampling method (classified mode only)                                                                                                                |
| `classify`         | `bool`          | `True`               | `True` = emit derived layers (`flood_fraction`, `quality_mask`, `permanent_water`); `False` = emit the native `ensemble_flood_extent` + `reference_water_mask` codes |
| `strategy`         | `str`           | `"peak"`             | One of `peak`, `aggregate`, or `all`                                                                                                                                 |
| `keep_processed`   | `bool`          | `True`               | Write processed GeoTIFFs to `processed/`                                                                                                                             |
| `peak_days_before` | `int`           | `0`                  | Window filter before the peak date                                                                                                                                   |
| `peak_days_after`  | `int`           | `0`                  | Window filter after the peak date                                                                                                                                    |
| `max_observations` | `int`           | `0`                  | Cap the number of returned dates after windowing                                                                                                                     |
| `peak_priority`    | `str`           | `"post"`             | Subsampling bias: `post`, `pre`, or `balanced`                                                                                                                       |

## Notes

- GFM has no download-versus-stream option like VIIRS or MODIS. It always
  loads Cloud-Optimised GeoTIFF assets via STAC discovery and `odc.stac`.
- Atlantis currently loads only `ensemble_flood_extent` and
  `reference_water_mask` from the upstream GFM collection.
- Upstream `advisory_flags`, `exclusion_mask`, and likelihood assets are not
  currently folded into `quality_mask`. In Atlantis, `quality_mask` means
  valid-observation coverage from the two loaded source layers, not a broader
  confidence or exclusion flag.
- For implementation details behind `search()`, `fetch()`, and `to_dataset()`, see
  [internals.md](internals.md) and the code in
  [src/atlantis/fetchers/gfm/**init**.py](../../src/atlantis/fetchers/gfm/__init__.py).
