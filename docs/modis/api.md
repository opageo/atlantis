# MODIS Python API

Python interface for the MODIS MCDWD flood fetcher. For CLI usage, see
[overview.md](overview.md). For the architectural breakdown, see
[internals.md](internals.md).

## Authentication

Both backends require an Earthdata Login bearer token. Register at
<https://urs.earthdata.nasa.gov/>, then export the token before running:

```bash
export EARTHDATA_TOKEN="YOUR_TOKEN_HERE"
```

Calling the fetcher without the variable raises
`atlantis.fetchers.modis.backend.MissingEarthdataTokenError`.

## Basic usage — LANCE streaming (NRT)

```python
from datetime import date
from pathlib import Path

from atlantis.fetchers.modis import MODISFetcher
from atlantis.models.event import FloodEvent

event = FloodEvent(
    event_id="lance_smoke",
    bbox=(66.0, 22.0, 72.0, 31.0),
    start_date=date(2026, 6, 4),
    end_date=date(2026, 6, 6),
)

fetcher = MODISFetcher(
    backend="lance_geotiff",  # default
    composite="F2",           # 2-day max-water composite (recommended default)
    classify=True,
    stream=True,              # /vsicurl/ + GDAL_HTTP_HEADERS bearer
)

results = fetcher.fetch(event, Path("data/lance_smoke"))
ds = fetcher.to_dataset(results[0])
print(int((ds["flood_fraction"] > 0).sum().item()), "flood pixels")
print("MODIS-only layers present:", "recurring_flood" in ds)
```

LANCE only retains files for ~1 week. For older dates, use the LAADS
backend below.

## Download mode (cache tiles locally)

```python
fetcher_download = MODISFetcher(
    backend="lance_geotiff",
    composite="F2",
    classify=True,
    stream=False,
)

download_results = fetcher_download.fetch(event, Path("data/lance_smoke_download"))
download_ds = fetcher_download.to_dataset(download_results[0])
```

Use this when you want local copies of the recent LANCE GeoTIFFs instead of
streaming them through `/vsicurl/`.

## Pakistan 2022 — LAADS HDF4 (historical)

The 2022 Pakistan floods fall in the VIIRS NOAA-S3 2021–2022 publication
gap. MODIS LAADS covers it cleanly:

```python
from datetime import date
from pathlib import Path

from atlantis.fetchers.modis import MODISFetcher
from atlantis.models.event import FloodEvent

event = FloodEvent(
    event_id="Pakistan_2022",
    bbox=(66.0, 22.0, 72.0, 31.0),
    start_date=date(2022, 8, 30),
    end_date=date(2022, 9, 1),
)

fetcher = MODISFetcher(
    backend="laads_hdf4",  # download HDF4 from LAADS (2003–2025 reprocessed)
    composite="F2",
    classify=True,
)

results = fetcher.fetch(event, Path("data/Pakistan_2022"))
peak = results[0]  # default strategy=peak
print("Peak date:", peak.date_token)

ds = fetcher.to_dataset(peak)
print("flood pixels:", int((ds["flood_fraction"] > 0).sum().item()))
```

## Raw mode (no classification)

```python
fetcher_raw = MODISFetcher(
    backend="lance_geotiff",
    composite="F2",
    classify=False,
    stream=True,
)

raw_results = fetcher_raw.fetch(event, Path("data/lance_smoke_raw"))
raw_ds = fetcher_raw.to_dataset(raw_results[0])
print(raw_ds["raw"].dtype, raw_ds["raw"].shape)
```

This preserves the original categorical codes (`0/1/2/3/255`) instead of
deriving the VIIRS-parity layers.

## KuroSiwo events

```python
from atlantis.utils.kurosiwo import build_kurosiwo_flood_events

events = build_kurosiwo_flood_events(
    Path("data/metadata/kurosiwo_metadata_v1.csv"),
    case="KuroSiwo_470",
    days_before=1,
    days_after=1,
)

fetcher = MODISFetcher(backend="laads_hdf4", composite="F2", classify=True)
results = fetcher.fetch(events[0], Path("data/KuroSiwo_470"))
```

This pattern uses the generic KuroSiwo event builder. MODIS does not have a
sensor-specific helper command like `fetch-kurosiwo-viirs`.

## Harmonisation

The harmoniser is source-aware — its default
`variable_resampling` already handles all MCDWD layers:

| Variable          | Resampler |
| ----------------- | --------- |
| `flood_fraction`  | `average` |
| `permanent_water` | `mode`    |
| `recurring_flood` | `mode`    |
| `quality_mask`    | `mode`    |
| `raw`             | `nearest` |

```python
from atlantis.harmoniser import Harmoniser, write_harmonised_raster

harmoniser = Harmoniser()
ds_harm = harmoniser.harmonise(ds, source_id="modis")
write_harmonised_raster(
    ds_harm["flood_fraction"], Path("harmonised/Pakistan_2022_modis.tif")
)
```

## Search diagnostics

```python
fetcher = MODISFetcher(backend="lance_geotiff", composite="F2")
matches = fetcher.search(event)
print(len(matches), "matches")
print(fetcher.last_diagnostics)
```

`last_diagnostics` records whether a search missed because of token/auth
issues, the LANCE retention window, empty listings, or tile mismatches.

## MODISFetcher parameters

| Parameter         | Type   | Default           | Description                                                                                                |
| ----------------- | ------ | ----------------- | ---------------------------------------------------------------------------------------------------------- |
| `backend`         | `str`  | `"lance_geotiff"` | `"lance_geotiff"` (NRT, streamable) or `"laads_hdf4"` (historical, download)                               |
| `composite`       | `str`  | `"F2"`            | One of `"F1"` / `"F1C"` / `"F2"` / `"F3"` (1-day, 1-day cloud-shadow screened, 2-day, 3-day)               |
| `classify`        | `bool` | `False`           | Decode raw codes into VIIRS-parity layers + `recurring_flood`; the CLI turns this on by default            |
| `stream`          | `bool` | `False`           | `/vsicurl/` streaming. Only valid with `lance_geotiff`; raises otherwise. The CLI turns this on by default |
| `strategy`        | `str`  | `"peak"`          | Multi-date reduction: `"peak"`, `"aggregate"`, `"all"`                                                     |
| `keep_processed`  | `bool` | `True`            | Write intermediate processed/ GeoTIFFs                                                                     |
| `base_url`        | `str`  | per-backend       | Override the backend's primary base URL                                                                    |
| `backup_base_url` | `str`  | `nrt4` mirror     | LANCE-only: secondary host used as fallback on connection error                                            |
| `timeout`         | `int`  | `300`             | HTTP request timeout (seconds)                                                                             |

Atlantis does not auto-select a MODIS composite from cloudiness or event
timing. The chosen `composite` is fixed for the whole fetch, and defaults to
`F2` unless you override it.

## Output layers (with `--classify` / `classify=True`)

| Variable          | MODIS class                 | Disk encoding                     |
| ----------------- | --------------------------- | --------------------------------- |
| `flood_fraction`  | `class == 3`                | uint8 percent (0–100), nodata=255 |
| `recurring_flood` | `class == 2` (Release 1.1+) | uint8 (0/1), nodata=0             |
| `permanent_water` | `class == 1`                | uint8 (0/1), nodata=0             |
| `quality_mask`    | `class != 255`              | uint8 (0/1), nodata=0             |

`flood_fraction` is binary 0/1 in memory — the harmoniser's `average`
resampling converts that into a true % flooded at 1 arcmin.

> **Pre-Release-1.1 archives.** Class `2` (recurring flood) is reserved
> but never populated in beta releases. To compare modern and legacy
> archives consistently, treat `{2, 3}` as a single flood mask:
>
> ```python
> flood_union = (ds["flood_fraction"] > 0) | (ds["recurring_flood"] > 0)
> ```

## HAND-masked terrain pitfall

MCDWD applies a [HAND mask](overview.md#hand-mask-post-compositing-since-beta-2--jan-2023)
**after** compositing: pixels in HAND-restricted terrain are reassigned
to `255` ("insufficient data") **even if water was detected**. This is
correct semantics for the product, but it has two consequences for
downstream code:

1. `quality_mask = (class != 255)` drops HAND-masked pixels — exactly
   the behaviour we want for confidence-aware analyses.
2. **Never** treat `255` as `0` (no flood). Doing so systematically
   under-reports flood at HAND boundaries. The companion `quality_mask`
   keeps the distinction explicit.

## Custom backend

```python
from atlantis.fetchers.modis.backend import (
    ModisBackend,
    ListingLocation,
    ModisListingEntry,
)

class MyBackend(ModisBackend):
    name = "my_backend"
    supports_streaming = True

    def get_listing_location(self, base_url, event_date, composite) -> ListingLocation: ...
    def get_directory_listing(self, base_url, location, timeout, *, headers=None) -> list[ModisListingEntry]: ...
    def find_remote_filename(self, h, v, composite, entries) -> ModisListingEntry | None: ...
    def build_result_url(self, base_url, location, entry) -> str: ...
```
