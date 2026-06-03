# Valencia 2024 CLI Examples

## Prerequisites

```bash
make setup   # install deps + restore data assets
```

Or run the Valencia demo directly: `uv run atlantis demo`

## Peak Strategy (default) — select the date with most flood pixels

```bash
uv run atlantis fetch \
  --event Valencia_2024 \
  --source viirs \
  --bbox "-1.5 38.8 0.5 40.0" \
  --start-date 2024-10-29 \
  --end-date 2024-11-04 \
  --plot \
  --harmonise \
  --output ./data/Valencia_2024
```

**Output:**

```
Fetching data for event: Valencia_2024
Sources: viirs
Output: data/Valencia_2024

Fetching from viirs...
  2024-10-29  flood_fraction  flooded=12 847  valid=38 400  fraction=0.334
  2024-10-30  flood_fraction  flooded= 9 211  valid=38 400  fraction=0.240
  2024-10-31  flood_fraction  flooded= 5 034  valid=38 400  fraction=0.131
  2024-11-01  flood_fraction  flooded= 1 872  valid=38 400  fraction=0.049
  Wrote 4 files
  Harmonised → Valencia_2024_2024-10-29_viirs_harmonised.tif
```

Generates:

- viirs/processed/ — 375 m classified GeoTIFFs for all dates
- viirs/plots/Valencia_2024_2024-10-29_viirs.png — peak date visualization
- viirs/harmonised/Valencia_2024_2024-10-29_viirs_harmonised.tif — 1 arcmin reprojected
- viirs/harmonised/Valencia_2024_2024-10-29_viirs_harmonised.png

## Other Strategies

**`--strategy aggregate`** — produces a temporal mean/mode composite across all dates in the window. Useful for understanding overall inundation patterns rather than peak extent. Single output set per event.

**`--strategy all`** — generates harmonised outputs for every date in the search window. Produces one harmonised GeoTIFF and PNG per date; useful for time-series analysis and animation.

**`--no-keep-processed`** — fetches and processes in memory only; skips writing the intermediate 375 m `processed/` GeoTIFFs. Only harmonised outputs are saved to disk. Saves significant storage for large regions.
