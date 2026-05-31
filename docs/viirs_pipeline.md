# VIIRS Pipeline Modes

Overview of the user-facing pipeline paths depending on flag combinations.

## Decision flowchart

```mermaid
flowchart TD
    START["atlantis fetch --source viirs"] --> SEARCH["Search NOAA S3 for tiles<br/>(bbox × date range)"]
    SEARCH --> STREAM{--stream?}

    STREAM -->|Yes| VSICURL["/vsicurl/ streaming<br/>(no local tiles)"]
    STREAM -->|No| DOWNLOAD["Download tiles to raw/"]

    VSICURL --> MOSAIC["Mosaic & clip to bbox"]
    DOWNLOAD --> MOSAIC

    MOSAIC --> CLASSIFY{--classify?}

    %% ─── Classify path ───────────────────────────────
    CLASSIFY -->|"Yes (default)"| CLASS_OUT["Classify pixels into layers"]
    CLASS_OUT --> LAYERS["flood_fraction — float32 [0.0–1.0]<br/>quality_mask — uint8 binary<br/>permanent_water — uint8 binary"]

    LAYERS --> HARM_ONLY{--harmonise-only?}
    HARM_ONLY -->|Yes| SKIP_PROC["Skip writing 375 m files"]
    HARM_ONLY -->|No| WRITE_PROC["Write processed/ GeoTIFFs<br/>(uint8, LZW, nodata=0 or 255)"]

    WRITE_PROC --> HARM{--harmonise?}
    HARM -->|No| DONE_PROC["Done — processed outputs only"]
    HARM -->|Yes| HARMONISE

    SKIP_PROC --> HARMONISE["Harmonise to 1-arcmin grid"]

    %% ─── Raw path ────────────────────────────────────
    CLASSIFY -->|"No (--no-classify)"| RAW_OUT["Keep raw integer codes<br/>(uint8, values 0–200)"]
    RAW_OUT --> WRITE_RAW["Write processed/<br/>*_raw.tif"]
    WRITE_RAW --> HARM_RAW{--harmonise?}
    HARM_RAW -->|No| DONE_RAW["Done — raw GeoTIFF only"]
    HARM_RAW -->|Yes| HARMONISE_RAW["Harmonise (nearest resampling)<br/>⚠️ Warning: codes preserved<br/>but not a continuous fraction"]

    %% ─── Harmonisation detail ────────────────────────
    HARMONISE --> HARM_STEPS["Reproject to EPSG:4326<br/>Resample flood_fraction (average)<br/>Resample masks (mode)"]
    HARM_STEPS --> HARM_WRITE["Write harmonised/ GeoTIFF<br/>uint8 pct [0–100], nodata=255"]
    HARM_WRITE --> PLOT{--plot?}
    PLOT -->|Yes| PNG["Write harmonised/ PNG"]
    PLOT -->|No| DONE_HARM["Done"]
    PNG --> DONE_HARM

    HARMONISE_RAW --> HARM_RAW_WRITE["Write harmonised/ GeoTIFF<br/>uint8 raw codes, nodata=255<br/>Normalisation skipped"]
    HARM_RAW_WRITE --> DONE_RAW_HARM["Done"]
```

## Mode summary

| Flags                       | Intermediate output                      | Final output                  | Flood variable                           |
| --------------------------- | ---------------------------------------- | ----------------------------- | ---------------------------------------- |
| _(defaults)_                | `processed/*_flood_fraction.tif` + masks | —                             | `flood_fraction` (uint8 pct, nodata=255) |
| `--harmonise`               | `processed/*_flood_fraction.tif` + masks | `harmonised/*_harmonised.tif` | `flood_fraction` (uint8 pct, nodata=255) |
| `--harmonise-only`          | _(none)_                                 | `harmonised/*_harmonised.tif` | `flood_fraction` (uint8 pct, nodata=255) |
| `--no-classify`             | `processed/*_raw.tif`                    | —                             | `raw` (uint8 codes 0–200)                |
| `--no-classify --harmonise` | `processed/*_raw.tif`                    | `harmonised/*_harmonised.tif` | `raw` (uint8 codes, nearest resampling)  |

## Data encoding at each stage

```
Raw tiles (NOAA S3)          uint8   codes 0–200         375 m
        │
        ▼
Processed (--classify)       uint8   flood pct 0–100     375 m, nodata=255
                             uint8   quality 0/1         375 m, nodata=0
                             uint8   perm. water 0/1     375 m, nodata=0
        │
        ▼
Harmonised                   uint8   flood pct 0–100     ~1 arcmin, nodata=255
                                     (average resampled)


Raw tiles (NOAA S3)          uint8   codes 0–200         375 m
        │
        ▼
Processed (--no-classify)    uint8   raw codes 0–200     375 m, nodata=0
        │
        ▼
Harmonised (raw)             uint8   raw codes 0–200     ~1 arcmin, nodata=255
                                     (nearest resampled, normalisation skipped)
```

## Notes

- **Harmonise-only** skips writing intermediate 375 m files — saves ~100 MB per event.
- **Raw + harmonise** uses nearest-neighbour resampling (preserves integer codes) but emits a warning that the result is not a continuous flood fraction.
- The normaliser's `skip_normalise_vars` set includes `"raw"` — raw codes are never min-max normalised even if passed through the full harmonisation pipeline.
- **Resampling methods** are configured in `variable_resampling`: `flood_fraction → average`, `quality_mask → mode`, `permanent_water → mode`, `raw → nearest`.
