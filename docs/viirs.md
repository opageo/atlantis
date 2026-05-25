# VIIRS Flood Detection

**Satellite-based flood mapping at 375m resolution**

Atlantis integrates VIIRS flood products from the JPSS (Joint Polar Satellite System) constellation—providing global flood detection derived from the Day-Night Band.

## What is VIIRS?

VIIRS (Visible Infrared Imaging Radiometer Suite) instruments aboard Suomi-NPP and NOAA-20 satellites detect floods at **375 meter resolution**. The flood products identify:

| Code | Meaning            |
| ---- | ------------------ |
| ≥160 | **Flood water** 🌊 |
| 99   | Open water         |
| 30   | Cloud cover ☁️     |
| 20   | Seasonal water     |
| 17   | Permanent water    |

Atlantis automatically classifies these into three outputs: flood extent, quality mask, and permanent water mask.

## Quick Start

### Fetch for a specific region

```bash
atlantis fetch \
  --event Yangtze_2020 \
  --source viirs \
  --bbox "105 28 125 38" \
  --start-date 2020-07-22 \
  --end-date 2020-07-22
```

That's it. Atlantis will:

1. **Search** VIIRS tiles intersecting your bbox
2. **Download** raw data from NOAA's JPSS archive
3. **Mosaic** multiple tiles if needed
4. **Clip** to your exact region
5. **Write** three GeoTIFF files:
   - `*_flood_extent.tif` — binary flood mask
   - `*_quality_mask.tif` — 0=cloud/water, 1=valid
   - `*_permanent_water.tif` — permanent water bodies

### Use the KuroSiwo catalogue

Working with flood events from the KuroSiwo SAR dataset? Fetch VIIRS for any case:

```bash
# Direct from catalogue
atlantis fetch-kurosiwo-viirs \
  --catalogue assets/ks_catalogue.gpkg \
  --case KuroSiwo_470

# Or pre-build metadata for faster iteration
atlantis build-kurosiwo-metadata \
  --catalogue assets/ks_catalogue.gpkg \
  --output data/metadata/kurosiwo.csv

atlantis fetch-kurosiwo-viirs \
  --metadata data/metadata/kurosiwo.csv \
  --case KuroSiwo_470
```

Add `--days-before 2 --days-after 2` to widen the temporal window around the flood event.

## Backends

Atlantis supports two VIIRS data sources:

| Backend      | Description                           | Default |
| ------------ | ------------------------------------- | ------- |
| `noaa_s3`    | NOAA JPSS S3 bucket (recommended)     | ✅      |
| `gmu_legacy` | GMU legacy archive (5-day composites) |         |

Switch backends:

```bash
# Via CLI flag
atlantis fetch --source viirs --backend gmu_legacy ...

# Or environment variable
export ATLANTIS_VIIRS_BACKEND=gmu_legacy
```

## Python API

Want to integrate VIIRS fetching into your workflow?

```python
from atlantis.fetchers.viirs import VIIRSFetcher
from atlantis.models.event import FloodEvent
from datetime import date

event = FloodEvent(
    event_id="my_flood",
    bbox=(105.0, 28.0, 125.0, 38.0),
    start_date=date(2020, 7, 22),
    end_date=date(2020, 7, 22),
    sources=["viirs"]
)

fetcher = VIIRSFetcher()

# Search only
results = fetcher.search(event)
print(f"Found {len(results)} tiles")

# Fetch and process
fetch_results = fetcher.fetch(event, output_dir="./data")

# Convert to xarray Dataset for analysis
dataset = fetcher.to_dataset(fetch_results[0])
print(dataset["flood_extent"].sum())  # Total flooded pixels
```

## Architecture

The VIIRS fetcher follows clean architecture principles with three focused components:

```
┌─────────────────────────────────────────────────────────┐
│                    VIIRSFetcher                         │
│              (orchestrates the flow)                    │
└─────────────┬───────────────────────┬───────────────────┘
              │                       │
              ▼                       ▼
┌─────────────────────┐    ┌──────────────────────┐
│   Backend Layer     │    │   Raster Processor   │
│                     │    │                      │
│ • NoaaS3Backend     │    │ • Mosaic tiles       │
│ • GmuLegacyBackend  │    │ • Clip to AOI        │
│                     │    │ • Classify pixels    │
│ Handles:            │    │ • Write outputs      │
│ • URL building      │    │                      │
│ • Directory listing │    │                      │
│ • File matching     │    │                      │
└─────────────────────┘    └──────────────────────┘
```

### Why this structure?

- **Backends** are swappable—add a new data source without touching processing code
- **Processor** is reusable—same raster logic regardless of where data comes from
- **Fetcher** stays simple—coordinates, doesn't implement

Need to add a backend? Implement the `ViirsBackend` protocol:

```python
from atlantis.fetchers.viirs_backend import ViirsBackend, ListingLocation

@register_backend  # (coming soon)
class MyCustomBackend(ViirsBackend):
    def get_listing_location(self, base_url, date, data_format) -> ListingLocation:
        ...

    def get_directory_links(self, base_url, location, timeout) -> list[str]:
        ...

    def find_remote_filename(self, aoi_id, entries) -> str | None:
        ...

    def build_result_url(self, base_url, location, filename) -> str:
        ...
```

## Output Format

All outputs are Cloud-Optimized GeoTIFFs (COG) with:

- **CRS**: EPSG:4326 (WGS84)
- **Dtype**: uint8
- **Compression**: LZW
- **Nodata**: 0

Perfect for direct use in GIS tools or loading with `rioxarray` / `rasterio`.

## Tips & Tricks

**Re-run without re-downloading**: Atlantis caches raw downloads, so subsequent runs are fast.

**Multiple dates**: The date range is inclusive—`--start-date 2020-07-20 --end-date 2020-07-22` fetches 3 days.

**Large regions**: VIIRS tiles are ~10°×10°. Large bboxes automatically trigger multi-tile mosaicing.

**Quality masks**: Always check `quality_mask`—VIIRS has cloud contamination like any optical sensor.

## Next Steps

- Explore the [architecture guide](../src/README.md) for the full pipeline vision
- Check out `notebooks/drafts/kurosiwo_viirs_showcase_cli.ipynb` for worked examples
- Read the [API reference](api.md) for detailed class documentation
