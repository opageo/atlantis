# MODIS Pipeline Modes

User-facing decision tree for the MODIS MCDWD fetcher CLI flags.

## Decision flowchart

```mermaid
flowchart TD
    START["atlantis fetch --source modis"] --> AUTH{"EARTHDATA_TOKEN<br/>set?"}
    AUTH -->|No| AUTH_FAIL["Empty fetch<br/>+ token hint"]
    AUTH -->|Yes| BACKEND{"--modis-backend?"}

    BACKEND -->|lance_geotiff<br/>(default)| LANCE["JSON listing<br/>nrt3 → nrt4 fallback"]
    BACKEND -->|laads_hdf4| LAADS["HTML listing<br/>MCDWD_L3 ≤2025<br/>MCDWD_L3_NRT ≥2026"]

    LANCE --> WINDOW{"Within ~1-week<br/>retention window?"}
    WINDOW -->|No| WINDOW_FAIL["Empty fetch<br/>+ suggest --modis-backend laads_hdf4"]
    WINDOW -->|Yes| LANCE_FETCH

    LAADS --> LAADS_FETCH

    subgraph LANCE_FETCH["LANCE materialise"]
        LF1{"--stream?"}
        LF2["/vsicurl/ + Bearer header"]
        LF3["Download .tif to raw/"]
        LF1 -->|on (default)| LF2
        LF1 -->|--no-stream| LF3
    end

    subgraph LAADS_FETCH["LAADS materialise"]
        LH1["Download .hdf to raw/"]
        LH2["Open HDF4 subdataset:<br/>Grid_Water_Composite:Flood_*Day_250m"]
        LH1 --> LH2
    end

    LANCE_FETCH --> MOSAIC
    LAADS_FETCH --> MOSAIC

    MOSAIC["Mosaic + clip to bbox"] --> STRATEGY{"--strategy?"}

    STRATEGY -->|peak (default)| PEAK["argmax flood_pixel_count"]
    STRATEGY -->|aggregate| AGG["nanmean flood_fraction<br/>+ mode masks"]
    STRATEGY -->|all| ALL["Per-date FetchResults"]

    PEAK --> KEEP{"--keep-processed?"}
    AGG --> KEEP
    ALL --> KEEP

    KEEP -->|on (default)| WRITE["Write processed/ GeoTIFFs<br/>(uint8, LZW)"]
    KEEP -->|--no-keep-processed| SKIP["Keep in memory only"]

    WRITE --> HARM{"--harmonise?"}
    SKIP --> HARM
    HARM -->|No| DONE["Done"]
    HARM -->|Yes| HARMONISE["Snap AOI → 1-arcmin global grid"]

    HARMONISE --> HARM_WRITE["Write harmonised/ GeoTIFF<br/>uint8 pct [0–100], nodata=255"]
    HARM_WRITE --> PLOT{"--plot?"}
    PLOT -->|Yes| PNG["Write plots/ PNG"]
    PLOT -->|No| DONE_HARM["Done"]
    PNG --> DONE_HARM
```

## Backend × strategy summary

| Backend         | Coverage                                  | Streaming | When to use                               |
| --------------- | ----------------------------------------- | --------- | ----------------------------------------- |
| `lance_geotiff` | NRT, ~1-week rolling window               | ✅        | Operational monitoring, recent events     |
| `laads_hdf4`    | 2003–2025 (MCDWD_L3) + 2026+ (NRT mirror) | ❌        | Historical mapping, ML labels, validation |

| Strategy    | When to use                                       | Output                                        |
| ----------- | ------------------------------------------------- | --------------------------------------------- |
| `peak`      | Single representative date for an event (default) | One `FetchResult`                             |
| `aggregate` | Smooth out cloud + observation gaps               | One `FetchResult` (`date_token="aggregated"`) |
| `all`       | Time-series analysis / day-by-day comparison      | N `FetchResults`                              |

## Streaming vs downloading

| Mode | Backend support | Disk usage | When to choose it |
| ---- | --------------- | ---------- | ----------------- |
| `--stream` | `lance_geotiff` only | Minimal | Recent NRT events where you want speed and no local cache |
| `--no-stream` | `lance_geotiff` and effectively required for `laads_hdf4` | Higher | Re-runs, HDF4 extraction, or environments where persistent local files are useful |

`laads_hdf4` is a download-first path because HDF4 is not practical for direct
range-read streaming. `lance_geotiff` can do either.

## Backends

### `lance_geotiff`

- Best for recent events inside the rolling NRT window.
- Supports `/vsicurl/` streaming with `GDAL_HTTP_HEADERS` bearer auth.
- Reads one GeoTIFF per composite tile, so it feels closest to the VIIRS user
    experience.

### `laads_hdf4`

- Best for historical mapping, benchmarking, and ML labels.
- Downloads HDF4 containers, then opens the requested flood subdataset.
- Covers the long archive (`MCDWD_L3`) and the mirrored NRT archive beyond the
    short LANCE retention window.

## Output encoding

```
Raw HDF4/GeoTIFF (LAADS / LANCE)   uint8 codes 0/1/2/3/255   ~250 m
        │
        ▼ classify
Processed (--classify)             uint8 percent flood        250 m, nodata=255
                                   uint8 0/1 quality          250 m, nodata=0
                                   uint8 0/1 permanent water  250 m, nodata=0
                                   uint8 0/1 recurring flood  250 m, nodata=0
        │
        ▼ harmonise
Harmonised                         uint8 percent flood        ~1 arcmin, nodata=255
                                   (average resampling on flood_fraction;
                                    mode for the masks)


Raw HDF4/GeoTIFF                   uint8 codes 0/1/2/3/255   ~250 m
        │
        ▼ --no-classify
Processed                          uint8 codes 0/1/2/3/255   250 m, nodata=255
        │
        ▼ harmonise
Harmonised (raw)                   uint8 codes 0/1/2/3/255   ~1 arcmin, nodata=255
                                   (nearest resampling — preserves classes)
```

## Strategies in detail (pixel-level)

### `peak` — argmax over pixel-count of class 3

```python
def flood_pixel_count(processed):
    if processed.flood_fraction is not None:
        return int((processed.flood_fraction > 0).sum())
    if processed.raw is not None:
        return int((processed.raw == 3).sum())
    return 0
```

The CLI also exposes a cloud-aware variant
(`atlantis.fetchers.modis.selection.cloud_aware_score`) that mirrors
[`estimate_modis_peak_dates.py`](https://github.com/gpbalsamo/ifs-floodbench/blob/main/Scripts/estimate_modis_peak_dates.py):

$$
\text{score} = \frac{\text{flood}}{\text{valid}} \times
\left(1 - \frac{\text{missing}}{\text{total}}\right)
$$

Useful when several dates have similar raw flood counts but differ
markedly in cloud cover.

### `aggregate` — nan-mean / mode

| Layer             | Reduction         | Why                             |
| ----------------- | ----------------- | ------------------------------- |
| `flood_fraction`  | `np.nanmean(...)` | Continuous → arithmetic mean    |
| `quality_mask`    | mode (uint8)      | Categorical 0/1 → most-frequent |
| `permanent_water` | mode              | Categorical                     |
| `recurring_flood` | mode              | Categorical                     |
| `raw`             | mode              | Categorical 0/1/2/3/255         |
| `cloud_fraction`  | scalar `np.mean`  | Scalar metadata                 |

The aggregated tile inherits the transform/CRS from the first date —
all dates share the same grid by construction.

### `all` — per-date FetchResults

No reduction. Each date's `ProcessedTile` becomes its own
`FetchResult`. Useful for temporal stacks; pair with
`--harmonise` to write one harmonised TIFF per date.

## HAND-masked pixels reminder

MCDWD's HAND mask reassigns pixels to `255` ("insufficient data") even
when water was detected — a deliberate algorithmic choice
([overview.md § HAND mask](overview.md#hand-mask-post-compositing-since-beta-2--jan-2023)).
This propagates through the pipeline as:

- `quality_mask = 0` for HAND-masked pixels (correct — these are not
  observations).
- `flood_fraction = 0` for HAND-masked pixels (because `class != 3`).

Downstream code should **never** map `255` to `0` (no flood); use
`quality_mask` to distinguish "observed not flooded" from "no
observation".
