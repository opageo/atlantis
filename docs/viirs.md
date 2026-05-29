# VIIRS Flood Detection

**Satellite-based flood mapping at 375 m resolution**

Atlantis integrates VIIRS flood products from the JPSS (Joint Polar Satellite System) constellation—providing global flood detection derived from the Day-Night Band.

## What is VIIRS?

VIIRS (Visible Infrared Imaging Radiometer Suite) instruments aboard Suomi-NPP and NOAA-20 satellites detect floods at **375 metre resolution**. The flood products encode integer pixel codes:

| Code | Meaning                                   |
| ---- | ----------------------------------------- |
| 0    | No data / fill                            |
| 1    | Land (no water)                           |
| 17   | Permanent water                           |
| 20   | Seasonal water                            |
| 30   | Cloud cover                               |
| 99   | Open water                                |
| ≥160 | **Flood water** (higher = more confident) |

By default Atlantis writes the raw pixel values as-is. Pass `--classify` to derive three binary layers: flood extent, quality mask, and permanent water mask.

## Quick Start

### Fetch for any location and date range

```bash
uv run atlantis fetch \
  --event Valencia_2024 \
  --source viirs \
  --bbox "-1.0 39.0 0.0 40.0" \
  --start-date 2024-10-29 \
  --end-date 2024-10-29
```

Atlantis will:

1. **Search** VIIRS AOI tiles intersecting your bbox
2. **Download** raw GeoTIFFs from the NOAA JPSS S3 bucket
3. **Mosaic** multiple tiles if the bbox spans more than one VIIRS tile
4. **Clip** the mosaic to your exact region
5. **Write** a single raw GeoTIFF:
   - `<event_id>_<date>_viirs_raw.tif` — integer pixel codes (0–255)

Add `--classify` to also write three derived layers:

```bash
uv run atlantis fetch \
  --event Valencia_2024 \
  --source viirs \
  --bbox "-1.0 39.0 0.0 40.0" \
  --start-date 2024-10-29 \
  --end-date 2024-10-29 \
  --classify
```

This produces:

- `*_flood_extent.tif` — binary flood mask (1 = flooded pixel)
- `*_quality_mask.tif` — 0 = cloud/fill, 1 = valid observation
- `*_permanent_water.tif` — permanent water bodies mask

### Use the KuroSiwo catalogue

Fetch VIIRS for any event from the KuroSiwo SAR flood dataset:

```bash
# Directly from the GeoPackage catalogue
uv run atlantis fetch-kurosiwo-viirs \
  --catalogue assets/ks_catalogue.gpkg \
  --case KuroSiwo_470

# Pre-build metadata CSV for faster repeated runs
uv run atlantis build-kurosiwo-metadata \
  --catalogue assets/ks_catalogue.gpkg \
  --output data/metadata/kurosiwo_metadata_v1.csv

uv run atlantis fetch-kurosiwo-viirs \
  --metadata data/metadata/kurosiwo_metadata_v1.csv \
  --case KuroSiwo_470
```

Widen the temporal window around the flood peak with `--days-before` / `--days-after`:

```bash
uv run atlantis fetch-kurosiwo-viirs \
  --catalogue assets/ks_catalogue.gpkg \
  --case KuroSiwo_470 \
  --days-before 2 \
  --days-after 2
```

Add `--classify` for derived layers:

```bash
uv run atlantis fetch-kurosiwo-viirs \
  --catalogue assets/ks_catalogue.gpkg \
  --case KuroSiwo_470 \
  --classify
```

Fetch all events (optionally limited):

```bash
# All KuroSiwo events
uv run atlantis fetch-kurosiwo-viirs \
  --catalogue assets/ks_catalogue.gpkg \
  --output /path/to/output

# First 5 events only
uv run atlantis fetch-kurosiwo-viirs \
  --catalogue assets/ks_catalogue.gpkg \
  --limit 5
```

### Output structure

```
<output>/
  <case_id>/
    viirs/
      raw/          # downloaded source tiles from NOAA S3
      processed/    # clipped, mosaicked GeoTIFF outputs
        <case_id>_<YYYYMMDD>_viirs_raw.tif
        # with --classify:
        <case_id>_<YYYYMMDD>_viirs_flood_extent.tif
        <case_id>_<YYYYMMDD>_viirs_quality_mask.tif
        <case_id>_<YYYYMMDD>_viirs_permanent_water.tif
```

## Backends

Two VIIRS data sources are supported:

| Backend      | Description                                                 | Default |
| ------------ | ----------------------------------------------------------- | ------- |
| `noaa_s3`    | NOAA JPSS public S3 bucket (`noaa-jpss`) — 1-day composites | ✅      |
| `gmu_legacy` | GMU legacy HTTP archive — 5-day composites                  |         |

Switch with `--viirs-backend`:

```bash
uv run atlantis fetch-kurosiwo-viirs \
  --catalogue assets/ks_catalogue.gpkg \
  --case KuroSiwo_470 \
  --viirs-backend gmu_legacy
```

Or set a default via environment variable:

```bash
export ATLANTIS_VIIRS_BACKEND=gmu_legacy
```

## Python API

```python
from pathlib import Path
from datetime import date

from atlantis.fetchers.viirs import VIIRSFetcher
from atlantis.models.event import FloodEvent

# ── Arbitrary event ───────────────────────────────────────────────────────────
event = FloodEvent(
    event_id="Valencia_2024",
    bbox=(-1.0, 39.0, 0.0, 40.0),   # west, south, east, north
    start_date=date(2024, 10, 29),
    end_date=date(2024, 10, 29),
)

fetcher = VIIRSFetcher()                     # raw mode (default)
fetch_results = fetcher.fetch(event, Path("data/viirs/Valencia_2024"))

# Load into xarray for analysis / plotting
ds = fetcher.to_dataset(fetch_results[0])
raw = ds["raw"]                              # xarray DataArray, CRS=EPSG:4326
print(raw.shape, raw.dtype)

# ── With classified outputs ───────────────────────────────────────────────────
fetcher_c = VIIRSFetcher(classify=True)
fetch_results_c = fetcher_c.fetch(event, Path("data/viirs/Valencia_2024_classified"))

ds_c = fetcher_c.to_dataset(fetch_results_c[0])
print(ds_c["flood_extent"].sum().item(), "flooded pixels")

# ── KuroSiwo event via metadata CSV ──────────────────────────────────────────
from atlantis.utils.kurosiwo import build_kurosiwo_flood_events

events = build_kurosiwo_flood_events(
    Path("data/metadata/kurosiwo_metadata_v1.csv"),
    case="KuroSiwo_470",
    days_before=1,
    days_after=1,
)
ks_results = fetcher.fetch(events[0], Path("data/viirs/KuroSiwo_470"))
ks_ds = fetcher.to_dataset(ks_results[0])
```

### Display a fetched raster

```python
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

viirs_codes = {
    1:   ("Land",             "#8B4513"),
    17:  ("Permanent water",  "#1f77b4"),
    20:  ("Seasonal water",   "#17becf"),
    30:  ("Cloud",            "#cccccc"),
    99:  ("Open water",       "#4682B4"),
    160: ("Flood (low)",      "#FFFF00"),
    200: ("Flood (high)",     "#FF0000"),
}

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

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    VIIRSFetcher                         │
│              (orchestrates the flow)                    │
└─────────────┬───────────────────────┬───────────────────┘
              │                       │
              ▼                       ▼
┌─────────────────────┐    ┌──────────────────────┐
│   Backend Layer     │    │  ViirsRasterProcessor │
│                     │    │                      │
│ • NoaaS3Backend     │    │ • Mosaic tiles       │
│ • GmuLegacyBackend  │    │ • Clip to AOI        │
│                     │    │ • Classify pixels    │
│ Handles:            │    │ • Write GeoTIFFs     │
│ • URL building      │    │                      │
│ • Directory listing │    │                      │
│ • Filename matching │    │                      │
└─────────────────────┘    └──────────────────────┘
```

Add a new data source by implementing the `ViirsBackend` abstract class:

```python
from atlantis.fetchers.viirs.backend import ViirsBackend, ListingLocation

class MyBackend(ViirsBackend):
    def get_listing_location(self, base_url, event_date, data_format) -> ListingLocation: ...
    def get_directory_links(self, base_url, location, timeout) -> list[str]: ...
    def find_remote_filename(self, aoi_id, entries) -> str | None: ...
    def build_result_url(self, base_url, listing_location, filename) -> str: ...
```

## Output Format

All outputs are GeoTIFFs with:

- **CRS**: EPSG:4326 (WGS84)
- **Dtype**: uint8
- **Compression**: LZW
- **Nodata**: 0

Compatible with `rioxarray`, `rasterio`, QGIS, and any GDAL-based tool.

## Tips & Tricks

**Re-run without re-downloading**: Raw tiles are cached in the `raw/` sub-directory; only processing is repeated on subsequent runs.

**Multiple dates**: The date range is inclusive—`--start-date 2024-10-27 --end-date 2024-10-31` fetches five daily composites.

**Large regions**: VIIRS tiles cover ~10°×10°. Large bboxes automatically trigger multi-tile mosaicing.

**Cloud contamination**: Always check the quality mask when using `--classify`—VIIRS is an optical sensor.

## Next Steps

- See `scripts/viirs_demo.py` for a runnable end-to-end example
- Explore `notebooks/drafts/kurosiwo_viirs_showcase_cli.ipynb` for an interactive walkthrough
- Read the [architecture guide](../src/README.md) for the full pipeline vision
