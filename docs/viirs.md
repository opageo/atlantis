# VIIRS Flood Detection

**Satellite-based flood mapping at 375 m resolution**

Atlantis integrates VIIRS flood products from the JPSS (Joint Polar Satellite System) constellation—providing global flood detection from the VIIRS Flood Mapping (VFM) product ([NOAA ATBD v1.0, 2021](https://www.star.nesdis.noaa.gov/jpss/documents/ATBD/ATBD_VIIRS_Flood_Mapping_v1.0.pdf)).

## What is VIIRS?

VIIRS (Visible Infrared Imaging Radiometer Suite) instruments aboard Suomi-NPP and NOAA-20 satellites detect floods at **375 metre resolution** using four Imager bands: I1 (visible, 0.64 µm), I2 (near-IR, 0.865 µm), I3 (shortwave-IR, 1.61 µm), and I5 (thermal IR, 11.45 µm).

### Pixel encoding

The GeoTIFFs on the NOAA S3 bucket use a simplified encoding (different from the [ATBD](https://www.star.nesdis.noaa.gov/jpss/documents/ATBD/ATBD_VIIRS_Flood_Mapping_v1.0.pdf)'s internal netCDF scheme):

| Code    | Meaning                                           |
| ------- | ------------------------------------------------- |
| 1       | Fill / No data (nodata sentinel)                  |
| 17      | Permanent water                                   |
| 20      | Seasonal water                                    |
| 30      | Cloud cover                                       |
| 99      | Open water                                        |
| 101–200 | **Flood water** — water fraction % = `code − 100` |
| ≥160    | High-confidence flood (≥60% water fraction)       |

> **Note:** The ATBD netCDF format uses different codes (e.g. 16=bare land, 17=vegetation). The GeoTIFF distribution simplifies these — land pixels are omitted, and codes 17/20/99 carry water-related meanings instead.

By default Atlantis classifies pixels into three binary layers: flood extent, quality mask, and permanent water mask. Pass `--no-classify` to write raw integer pixel codes instead.

## Quick start

```bash
uv run atlantis fetch \
  --event valencia_2024 \
  --source viirs \
  --bbox "-1.2 39.0 0.2 39.8" \
  --start-date 2024-10-30 \
  --end-date 2024-11-01 \
  --harmonise-only
```

This streams VIIRS tiles from NOAA S3, classifies flood pixels, and writes only the final harmonised 1-arcmin GeoTIFF + PNG:

```
harmonised/
  valencia_2024_2024-10-31_viirs_harmonised.tif   # uint8, 1 arcmin, flood % [0–100], nodata=255
  valencia_2024_2024-10-31_viirs_harmonised.png
```

## CLI reference

### `atlantis fetch --source viirs`

Fetch VIIRS flood data for any location and date range.

```bash
uv run atlantis fetch \
  --event <event_id> \
  --source viirs \
  --bbox "<west> <south> <east> <north>" \
  --start-date YYYY-MM-DD \
  --end-date YYYY-MM-DD \
  [flags]
```

### `atlantis fetch-kurosiwo-viirs`

Fetch VIIRS for events from the KuroSiwo SAR flood catalogue (bbox and dates resolved automatically):

```bash
uv run atlantis fetch-kurosiwo-viirs \
  --catalogue assets/ks_catalogue.gpkg \
  --case KuroSiwo_470 \
  --harmonise-only
```

### `atlantis harmonise --source viirs`

Resample previously fetched VIIRS outputs to a uniform 1-arcmin grid:

```bash
uv run atlantis harmonise \
  --event valencia_2024 \
  --source viirs
```

## Flags

### Output control

| Flag               | Default | Effect                                                              |
| ------------------ | ------- | ------------------------------------------------------------------- |
| `--classify`       | on      | Produce flood/quality/water binary masks instead of raw pixel codes |
| `--no-classify`    |         | Write raw integer pixel codes (single GeoTIFF)                      |
| `--harmonise`      | off     | Also produce a resampled 1-arcmin flood-fraction GeoTIFF            |
| `--harmonise-only` | off     | Write only the harmonised output (no intermediate 375 m files)      |
| `--plot`           | off     | Save a PNG of the peak-flood date                                   |

### Data access

| Flag              | Default   | Effect                                                   |
| ----------------- | --------- | -------------------------------------------------------- |
| `--stream`        | on        | Stream tiles from S3 via `/vsicurl/` (no local download) |
| `--no-stream`     |           | Download tiles to `raw/` for reuse across runs           |
| `--viirs-backend` | `noaa_s3` | Data source (`noaa_s3` or `gmu_legacy`)                  |

### Flood threshold

| Flag                    | Effect                               |
| ----------------------- | ------------------------------------ |
| `--flood-threshold 101` | Most inclusive — all flood pixels    |
| `--flood-threshold 160` | **Default** — ≥60% water fraction    |
| `--flood-threshold 180` | Most stringent — ≥80% water fraction |

The flood range is 101–200, encoding water fraction (101 = 1%, 200 = 100%). Pixels with codes 1, 17, 20, 30, 99 are never counted as flood.

### KuroSiwo-specific

| Flag            | Default | Effect                                          |
| --------------- | ------- | ----------------------------------------------- |
| `--catalogue`   |         | Path to KuroSiwo GeoPackage                     |
| `--metadata`    |         | Path to pre-built metadata CSV (faster lookups) |
| `--case`        |         | Single case ID (omit to fetch all events)       |
| `--limit`       |         | Process only the first N events                 |
| `--days-before` | 1       | Days before flood peak to include               |
| `--days-after`  | 1       | Days after flood peak to include                |

### Harmonisation

| Flag                  | Default | Effect                                      |
| --------------------- | ------- | ------------------------------------------- |
| `--target-resolution` | 0.0167° | Target grid spacing (1 arcmin default)      |
| `--dry-run`           |         | Show what would be processed without acting |

> **Tip:** The harmoniser uses `average` resampling for flood extent (produces a flood-fraction) and `mode` for binary masks. Override via `ATLANTIS_VARIABLE_RESAMPLING` in `.env`.

## Streaming vs downloading

VIIRS tiles (~20 MB each) are streamed directly from NOAA S3 via GDAL's `/vsicurl/` driver by default. This is ideal when disk space is limited or you only need a single run, since no `raw/` directory is created.

| Mode     | Flag          | Disk usage (typical)      | Network dependency             |
| -------- | ------------- | ------------------------- | ------------------------------ |
| Stream   | `--stream`    | 0 (processed output only) | Required throughout processing |
| Download | `--no-stream` | ~50–200 MB raw tiles      | Only during fetch              |

> Streaming works with the `noaa_s3` backend only (the default). Use `--no-stream` if you need `gmu_legacy`.

## Backends

| Backend      | Description                                                 | Default |
| ------------ | ----------------------------------------------------------- | ------- |
| `noaa_s3`    | NOAA JPSS public S3 bucket (`noaa-jpss`) — 1-day composites | ✅      |
| `gmu_legacy` | GMU legacy HTTP archive — 5-day composites                  |         |

Set via `--viirs-backend` or the environment variable `ATLANTIS_VIIRS_BACKEND`.

## Output structure

```
<output>/
  <event_id>/
    viirs/
      raw/          # only with --no-stream
      processed/    # absent with --harmonise-only
        # --classify (default):
        <event_id>_<YYYYMMDD>_viirs_flood_extent.tif
        <event_id>_<YYYYMMDD>_viirs_quality_mask.tif
        <event_id>_<YYYYMMDD>_viirs_permanent_water.tif
        # --no-classify:
        <event_id>_<YYYYMMDD>_viirs_raw.tif
      plots/        # with --plot
        <event_id>_<YYYY-MM-DD>_viirs.png
      harmonised/   # with --harmonise or --harmonise-only
        <event_id>_<YYYY-MM-DD>_viirs_harmonised.tif
        <event_id>_<YYYY-MM-DD>_viirs_harmonised.png
```

## Output format

All GeoTIFFs share these properties:

- **CRS**: EPSG:4326 (WGS84)
- **Dtype**: uint8
- **Compression**: LZW
- **Nodata**: 0 (processed) / 255 (harmonised)

Harmonised flood extent values are stored as **integer percentages** (0–100),
where 0 = no flood and 100 = fully flooded. This gives 1% precision while
using 4× less disk space than float32.

Compatible with `rioxarray`, `rasterio`, QGIS, and any GDAL-based tool.

## Tips

- **Multiple dates** — The date range is inclusive: `--start-date 2024-10-27 --end-date 2024-10-31` fetches five daily composites.
- **Large regions** — VIIRS tiles cover ~10°×10°. Large bboxes automatically trigger multi-tile mosaicing.
- **Cloud contamination** — Always check the quality mask. VIIRS is an optical sensor and cloud cover masks flood signal.
- **Re-run without re-fetching** — With `--no-stream`, raw tiles are cached in `raw/` and reused on subsequent runs.
- **Batch KuroSiwo runs** — Use `--harmonise-only` to save ~100 MB of intermediate files per event.
- **Pre-build metadata** — Run `atlantis build-kurosiwo-metadata` once to speed up repeated `fetch-kurosiwo-viirs` calls.

## Further reading

- [Pipeline modes & flowchart](viirs_pipeline.md)
- [Python API reference](viirs_api.md)
- [Architecture & internals](viirs_internals.md)
- [Pipeline vision](../src/README.md)
- `scripts/viirs_demo.py` — runnable end-to-end example
- `notebooks/drafts/kurosiwo_viirs_showcase_cli.ipynb` — interactive walkthrough
