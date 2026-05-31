# VIIRS Python API

Python interface for the VIIRS flood fetcher. For CLI usage, see [viirs.md](viirs.md).

## Basic usage

```python
from pathlib import Path
from datetime import date

from atlantis.fetchers.viirs import VIIRSFetcher
from atlantis.models.event import FloodEvent

event = FloodEvent(
    event_id="valencia_2024",
    bbox=(-1.2, 39.0, 0.2, 39.8),   # west, south, east, north
    start_date=date(2024, 10, 30),
    end_date=date(2024, 11, 1),
)

fetcher = VIIRSFetcher()                     # stream=True, classify=True by default
fetch_results = fetcher.fetch(event, Path("data/viirs/valencia_2024"))

# Load into xarray for analysis / plotting
ds = fetcher.to_dataset(fetch_results[0])
flood = ds["flood_extent"]                   # xarray DataArray, CRS=EPSG:4326
print(flood.sum().item(), "flooded pixels")
```

## Raw mode (no classification)

```python
fetcher_raw = VIIRSFetcher(classify=False)
fetch_results_raw = fetcher_raw.fetch(event, Path("data/viirs/valencia_2024_raw"))

ds_raw = fetcher_raw.to_dataset(fetch_results_raw[0])
print(ds_raw["raw"].shape, ds_raw["raw"].dtype)
```

## Download mode (cache tiles locally)

```python
fetcher_download = VIIRSFetcher(stream=False)
download_results = fetcher_download.fetch(event, Path("data/viirs/valencia_2024"))
download_ds = fetcher_download.to_dataset(download_results[0])
```

## KuroSiwo events

```python
from atlantis.utils.kurosiwo import build_kurosiwo_flood_events

events = build_kurosiwo_flood_events(
    Path("data/metadata/kurosiwo_metadata_v1.csv"),
    case="KuroSiwo_470",
    days_before=1,
    days_after=1,
)
ks_results = fetcher.fetch(events[0], Path("data/viirs/KuroSiwo_470"))
ks_ds = fetcher.to_dataset(ks_results[0])
print(ks_ds["flood_extent"].sum().item(), "flooded pixels")
```

## Harmonisation

```python
from atlantis.harmoniser import Harmoniser, write_harmonised_raster

harmoniser = Harmoniser()  # defaults to 1 arcmin target
ds_harm = harmoniser.harmonise(ds, source_id="viirs")
print(ds_harm["flood_extent"].dtype, ds_harm["flood_extent"].shape)
# float32 in-memory, ~6% of original pixels

# Write to disk as uint8 percentage [0–100], nodata=255
write_harmonised_raster(ds_harm["flood_extent"], Path("harmonised/output.tif"))
```

## Displaying raw pixel codes

```python
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

viirs_codes = {
    1:   ("Fill / No data",   "#000000"),
    17:  ("Permanent water",  "#1f77b4"),
    20:  ("Seasonal water",   "#17becf"),
    30:  ("Cloud",            "#cccccc"),
    99:  ("Open water",       "#4682B4"),
    130: ("Flood (30% frac)", "#ffeb3b"),
    160: ("Flood (60% frac)", "#FF9800"),
    200: ("Flood (100%)",     "#FF0000"),
}

fetcher_raw = VIIRSFetcher(classify=False)
ds_raw = fetcher_raw.to_dataset(fetcher_raw.fetch(event, Path("data/viirs/raw"))[0])
raw = ds_raw["raw"]

fig, (ax, ax_leg) = plt.subplots(1, 2, figsize=(14, 7),
                                  gridspec_kw={"width_ratios": [3, 1]})
raw.plot(ax=ax, cmap="turbo", add_colorbar=True)
ax.set_title("VIIRS raw composite (375 m)")

patches = [Patch(facecolor=c, label=f"{k}: {l}") for k, (l, c) in viirs_codes.items()]
ax_leg.legend(handles=patches, loc="center", title="Pixel codes")
ax_leg.axis("off")
plt.tight_layout()
plt.show()
```

## VIIRSFetcher parameters

| Parameter        | Type   | Default     | Description                                         |
| ---------------- | ------ | ----------- | --------------------------------------------------- |
| `stream`         | `bool` | `True`      | Stream tiles via `/vsicurl/` instead of downloading |
| `classify`       | `bool` | `True`      | Decode raw codes into flood/quality/water masks     |
| `flood_min_code` | `int`  | `160`       | Minimum code to count as flood (range: 101–200)     |
| `backend`        | `str`  | `"noaa_s3"` | Data backend (`"noaa_s3"` or `"gmu_legacy"`)        |

## Adding a custom backend

```python
from atlantis.fetchers.viirs.backend import ViirsBackend, ListingLocation

class MyBackend(ViirsBackend):
    def get_listing_location(self, base_url, event_date, data_format) -> ListingLocation: ...
    def get_directory_links(self, base_url, location, timeout) -> list[str]: ...
    def find_remote_filename(self, aoi_id, entries) -> str | None: ...
    def build_result_url(self, base_url, listing_location, filename) -> str: ...
```
