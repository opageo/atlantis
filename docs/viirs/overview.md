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
| 17      | Vegetation                                        |
| 20      | Snow / ice                                        |
| 30      | Cloud cover                                       |
| 99      | Permanent water (NOAA NormalWater reference)      |
| 101–200 | **Flood water** — water fraction % = `code − 100` |
| ≥160    | High-confidence flood (≥60% water fraction)       |

> **Note:** The authoritative legend is embedded in each NOAA GeoTIFF as the band tag `WaterDetection#TypeDescription`. The table above mirrors that tag for the classes Atlantis decodes; additional classes (16=Bareland, 27=River/lake ice, 38=mixed snow/ice/water, 50=Shadow) are present in the source data but currently pass through as `0` flood fraction with no dedicated layer.

By default Atlantis decodes raw VIIRS codes into a continuous `flood_fraction` layer plus `quality_mask` and `permanent_water` masks. Pass `--no-classify` to write raw integer pixel codes instead.

## Quick start

```bash
uv run atlantis fetch \
  --event valencia_2024 \
  --source viirs \
  --bbox "-1.2 39.0 0.2 39.8" \
  --start-date 2024-10-30 \
  --end-date 2024-11-01 \
  --no-keep-processed --harmonise
```

This streams VIIRS tiles from NOAA S3, derives per-pixel flood fraction plus quality and permanent-water masks, and writes the final harmonised 1-arcmin GeoTIFF (in `harmonised/`) alongside its PNG visualisation (in `plots/`):

```
harmonised/
  valencia_2024_2024-10-31_viirs_harmonised.tif   # uint8, 1 arcmin, flood % [0–100], nodata=255
plots/
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
  --no-keep-processed --harmonise
```

### `atlantis harmonise --source viirs`

Resample previously fetched VIIRS outputs to a uniform 1-arcmin grid:

```bash
uv run atlantis harmonise \
  --event valencia_2024 \
  --source viirs
```

## Flags

### `atlantis fetch --source viirs` flags

#### Output control

| Flag                  | Default | Effect                                                                                                                                                                                   |
| --------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--classify`          | on      | Produce classified `flood_fraction`, `quality_mask`, and `permanent_water` layers instead of raw pixel codes                                                                             |
| `--no-classify`       |         | Write raw integer pixel codes (single GeoTIFF)                                                                                                                                           |
| `--harmonise`         | off     | Also produce a resampled 1-arcmin flood-fraction GeoTIFF                                                                                                                                 |
| `--no-keep-processed` | off     | Skip writing intermediate 375 m GeoTIFFs; keep processed rasters in memory unless combined with `--harmonise` and/or `--plot`                                                            |
| `--plot`              | off     | Save a PNG of the peak-flood date                                                                                                                                                        |
| `--strategy`          | `peak`  | Multi-date reduction: `peak` (most-flooded date), `aggregate` (mean/mode composite), `all` (per-date outputs). See [Strategies in detail](pipeline.md#strategies-in-detail-pixel-level). |

#### Peak-window filtering and subsampling

These flags work as **composable modifiers** on top of any `--strategy`. They are
no-ops for `--strategy peak` (which always returns a single date).

| Flag                 | Default | Effect                                                                                                                                         |
| -------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `--peak-days-before` | `0`     | Include dates up to N days **before** the computed peak. 0 = no window filter.                                                                 |
| `--peak-days-after`  | `0`     | Include dates up to N days **after** the computed peak. 0 = no window filter.                                                                  |
| `--peak-window-days` | `0`     | Symmetric shorthand: sets both `--peak-days-before` and `--peak-days-after` to the same value. Cannot be combined with the two specific flags. |
| `--max-observations` | `0`     | Cap the number of returned dates after windowing. 0 = no limit. Selection order is controlled by `--peak-priority`.                            |
| `--peak-priority`    | `post`  | Subsampling bias: `post` (post-event first, then pre), `pre` (pre-event first, then post), `balanced` (alternating ±1, ±2, …).                 |

The **peak** is the date with the highest flood-pixel count among all fetched dates
(ties broken by earliest). Windowing always keeps the peak date.

**Example — 7-day window, up to 5 observations (post-event bias):**

```bash
uv run atlantis fetch \
  --event valencia_2024 \
  --source viirs \
  --bbox "-1.2 39.0 0.2 39.8" \
  --start-date 2024-10-20 \
  --end-date 2024-11-10 \
  --strategy all \
  --peak-window-days 7 \
  --max-observations 5 \
  --no-keep-processed --harmonise
```

This fetches all dates in the two-week window, identifies the peak, filters to ±7
days around it, then keeps the peak plus the 4 nearest post-event dates (the
`post` default). The 5 harmonised GeoTIFFs are written to `harmonised/`.

**Interaction with strategies:**

| Strategy    | Window filter                         | Subsampling                                |
| :---------- | :------------------------------------ | :----------------------------------------- |
| `peak`      | No-op (1 date returned regardless)    | No-op                                      |
| `aggregate` | Narrows the composite to window dates | Narrows further before mean/mode reduction |
| `all`       | Filters the returned FetchResult list | Subsamples the filtered list               |

#### Data access

| Flag              | Default   | Effect                                                   |
| ----------------- | --------- | -------------------------------------------------------- |
| `--stream`        | on        | Stream tiles from S3 via `/vsicurl/` (no local download) |
| `--no-stream`     |           | Download tiles to `raw/` for reuse across runs           |
| `--viirs-backend` | `noaa_s3` | Data source (`noaa_s3` or `gmu_legacy`)                  |

With `--classify`, codes `101–200` become `flood_fraction` values in `[0.01, 1.00]` in memory and are written as uint8 percentages `[1, 100]` on disk. Codes `1`, `17`, `20`, `30`, and `99` contribute `0` flood fraction; `quality_mask` and `permanent_water` are written as companion masks.

### `atlantis fetch-kurosiwo-viirs` flags

#### KuroSiwo-specific

| Flag            | Default | Effect                                          |
| --------------- | ------- | ----------------------------------------------- |
| `--catalogue`   |         | Path to KuroSiwo GeoPackage                     |
| `--metadata`    |         | Path to pre-built metadata CSV (faster lookups) |
| `--case`        |         | Single case ID (omit to fetch all events)       |
| `--limit`       |         | Process only the first N events                 |
| `--days-before` | 0       | Days before flood peak to include               |
| `--days-after`  | 0       | Days after flood peak to include                |

### `atlantis harmonise --source viirs` flags

#### Harmonisation

| Flag                  | Default | Effect                                      |
| --------------------- | ------- | ------------------------------------------- |
| `--target-resolution` | 0.0167° | Target grid spacing (1 arcmin default)      |
| `--dry-run`           |         | Show what would be processed without acting |

> **Tip:** The harmoniser uses `average` resampling for `flood_fraction` and `mode` for the mask layers. Override via `ATLANTIS_VARIABLE_RESAMPLING` in `.env`.

## Streaming vs downloading

VIIRS tiles (~20 MB each) are streamed directly from NOAA S3 via GDAL's `/vsicurl/` driver by default. This is ideal when disk space is limited or you only need a single run, since no `raw/` directory is created.

| Mode     | Flag          | Disk usage (typical)      | Network dependency             |
| -------- | ------------- | ------------------------- | ------------------------------ |
| Stream   | `--stream`    | 0 (processed output only) | Required throughout processing |
| Download | `--no-stream` | ~50–200 MB raw tiles      | Only during fetch              |

> Streaming works with the `noaa_s3` backend only (the default). Use `--no-stream` if you need `gmu_legacy`.

## Backends

Both backends serve the same underlying science product — the **VIIRS Flood
Mapping (VFM)** algorithm developed at George Mason University (Li & Sun) under
the JPSS Proving Ground programme ([ATBD v1.0, 2021](https://www.star.nesdis.noaa.gov/jpss/documents/ATBD/ATBD_VIIRS_Flood_Mapping_v1.0.pdf)).
They differ in _who hosts the bytes_, _which compositing window_ is exposed, and
_how the directory layout is structured_.

| Backend      | Host & protocol                                                                             | Composite window | Default |
| ------------ | ------------------------------------------------------------------------------------------- | ---------------- | ------- |
| `noaa_s3`    | NOAA NODD public S3 bucket `s3://noaa-jpss/` (HTTPS, anonymous)                             | **1-day** global | ✅      |
| `gmu_legacy` | GMU JPSS Flood archive `https://jpssflood.gmu.edu/downloads/pub/` (HTTP directory listings) | **5-day** global |         |

Set via `--viirs-backend` or the environment variable `ATLANTIS_VIIRS_BACKEND`.

### `noaa_s3` — NOAA JPSS on AWS (recommended)

- **Source of truth** — NOAA Open Data Dissemination (NODD) programme, AWS registry entry [`noaa-jpss`](https://registry.opendata.aws/noaa-jpss/).
- **Bucket / region** — `arn:aws:s3:::noaa-jpss` in `us-east-1`. Browse at [noaa-jpss.s3.amazonaws.com](https://noaa-jpss.s3.amazonaws.com/index.html).
- **Atlantis path** — `JPSS_Blended_Products/VFM_1day_GLB/TIF/<YYYY>/<MM>/<DD>/`.
- **Tile naming** — `VIIRS-Flood-1day-GLB<AOI>_v2r0_blend_s<start>_e<end>_c<created>.tif` (e.g. `GLB001`…`GLB145`). The `blend` token indicates blended Suomi-NPP + NOAA-20 observations.
- **AOI grid** — 136 land-covering 10°×10° tiles defined by the VFM algorithm (see `src/atlantis/fetchers/viirs/data/viirs_aois.geojson`). On a typical day ~145 tile files are present (some AOIs split at the antimeridian).
- **Spatial / temporal** — 375 m, EPSG:4326, uint8 pixel codes 0–200, daily global.
- **Other VFM variants on the same bucket** — `VFM_5day_GLB/` (5-day composite), `VIIRS-ABI-Flood-Day/`, `VIIRS-ABI-Flood-Day-TIF/`, `VIIRS-ABI-Flood-Day-Shapefiles/` (VIIRS+GOES-ABI joint daytime product). Atlantis currently consumes only `VFM_1day_GLB/TIF/`.
- **License** — open NOAA data (NODD terms of use; attribution requested).
- **Streaming** — works end-to-end via GDAL `/vsicurl/` because S3 supports HTTP range reads.
- **Docs** — [NOAA-Big-Data-Program/nodd-data-docs (JPSS)](https://github.com/NOAA-Big-Data-Program/nodd-data-docs/tree/main/JPSS).

### `gmu_legacy` — George Mason University archive

- **Source of truth** — Hosted by the same group that authored the VFM algorithm (Sanmei Li, Donglian Sun et al., George Mason University). This was the original public distribution point before the NOAA NODD migration.
- **Atlantis path** — `https://jpssflood.gmu.edu/downloads/pub/<YYYYMMDD>/tif/`.
- **Tile naming** — files matching `_005day_<AOI>.tif` or `_005day_<AOI>.tif.zip` (the `005day` token is the **5-day max-water-fraction composite**, used to suppress cloud and shadow contamination across the compositing window).
- **AOI grid** — same 136 10°×10° AOI scheme as `noaa_s3`.
- **Streaming** — _not supported_. `.zip`-packaged tiles and a plain HTML directory listing require the `--no-stream` (download-and-extract) path.
- **Coverage** — the GMU site does not advertise its index; Atlantis does not declare published years and falls back to per-date probing. Reachability is best-effort: the host may be intermittently offline (it was unreachable at the time of writing).
- **When to use it** — historical 5-day composites, or as a fallback for the years currently missing from NOAA S3 (see below). For modern operational use, prefer `noaa_s3`.

### Data availability

VIIRS coverage differs per backend. Atlantis queries the backend's published
years before fetching and aborts early with an explanation if the requested
window falls outside that range.

| Backend      | Published calendar years (as of 2026-06) | Notes                                                                                                                                               |
| ------------ | ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `noaa_s3`    | 2012–2020, 2023–2026                     | **2021 and 2022 are not published** on the public NOAA JPSS bucket. Verified via S3 listing of `JPSS_Blended_Products/VFM_1day_GLB/TIF/`.           |
| `gmu_legacy` | Best-effort (not declared)               | The GMU archive does not enumerate cheaply; Atlantis attempts each requested date directly. May cover the 2021–2022 gap when the host is reachable. |

The `noaa_s3` coverage set is read at runtime from `JPSS_Blended_Products/VFM_1day_GLB/<FORMAT>/`,
so it will pick up new years automatically as NOAA publishes them. When a request
targets a year that NOAA does not publish (e.g. the 2022 Pakistan floods), the
CLI emits a clear diagnostic and suggests falling back to `--viirs-backend gmu_legacy`.

#### Where are 2021 and 2022 on AWS?

Nowhere. The 2021–2022 gap is **bucket-wide on the public NOAA mirror**, not specific to
`VFM_1day_GLB/TIF`. We verified every plausible alternative S3 location and all
exhibit the same gap:

| Bucket / prefix                                                            | Years present        |
| -------------------------------------------------------------------------- | -------------------- |
| `s3://noaa-jpss/JPSS_Blended_Products/VFM_1day_GLB/{TIF,NETCDF,SHAPEZIP}/` | 2012–2020, 2023–2026 |
| `s3://noaa-jpss/JPSS_Blended_Products/VFM_5day_GLB/{TIF,NETCDF,PNG}/`      | 2012–2020, 2023–2026 |
| `s3://noaa-jpss/JPSS_Blended_Products/SNPP_DECOM/NetCDF/`                  | 2018–2020 only       |
| `s3://noaa-jpss/JPSS_Blended_Products/VIIRS-ABI-Flood-Day*/` (3 variants)  | 2025–2026 only       |
| `s3://noaa-nesdis-{n20,snpp}-pds/VIIRS_VFM_MWS_MOSAIC/`                    | 2024–2026 only       |

For events in 2021–2022 the only routes are:

- **GMU JPSS Flood archive** (`gmu_legacy` backend) — the historical
  distribution at `jpssflood.gmu.edu`. The host is intermittently reachable;
  retry from a non-cloud network if requests time out.
- **NESDIS Product Distribution & Access** — authenticated portal at
  <https://www.star.nesdis.noaa.gov/jpss/VIIRSflood.php>. Not yet wired into
  Atlantis (account + credentials required).
- **NOAA NCEI archive** — long-term mirror, requires manual ordering.

**Pakistan 2022 — GMU Legacy backend example:**

```bash
uv run atlantis fetch \
  --event Pakistan_2022 \
  --source viirs \
  --bbox "67.5 26 70 29.5" \
  --start-date 2022-08-28 --end-date 2022-09-03 \
  --viirs-backend gmu_legacy --no-stream \
  --plot --harmonise --no-keep-processed \
  --output ./data/Pakistan_2022
```

See [`CLI_Examples.md`](../../CLI_Examples.md#viirs-availability-notes) for more
details on backend selection.

## Output structure

```
<output>/
  <event_id>/
    viirs/
      raw/          # only with --no-stream
      processed/    # absent with --no-keep-processed
        # --classify (default):
        <event_id>_<YYYYMMDD>_viirs_flood_fraction.tif
        <event_id>_<YYYYMMDD>_viirs_quality_mask.tif
        <event_id>_<YYYYMMDD>_viirs_permanent_water.tif
        # --no-classify:
        <event_id>_<YYYYMMDD>_viirs_raw.tif
      plots/        # with --plot, or with --harmonise (harmonised PNG goes here too)
        <event_id>_<YYYY-MM-DD>_viirs.png
        <event_id>_<YYYY-MM-DD>_viirs_harmonised.png
      harmonised/   # with --harmonise
        <event_id>_<YYYY-MM-DD>_viirs_harmonised.tif
```

## Output format

All GeoTIFFs share these properties:

- **CRS**: EPSG:4326 (WGS84)
- **Dtype**: uint8
- **Compression**: LZW
- **Nodata**: `255` for `processed/*_flood_fraction.tif` and `harmonised/*.tif`; `0` for `processed/*_raw.tif`, `*_quality_mask.tif`, and `*_permanent_water.tif`

Processed and harmonised flood-fraction values are stored as **integer percentages** (0–100),
where 0 = no flood and 100 = fully flooded. This gives 1% precision while
using 4× less disk space than float32.

Compatible with `rioxarray`, `rasterio`, QGIS, and any GDAL-based tool.

## Canonical 1-arcmin global grid

Harmonised outputs are aligned to a **fixed global reference grid** so that
every AOI we produce is a bit-for-bit subset of the same worldwide raster.
The reference is the grid used by ECMWF's `Globe_flood_area_*.grb`
(`s3://atlantis/`), shared with several other 1-arcmin global datasets
(MERIT/CaMa-Flood, etc.).

### What "1 arcmin on the global grid" means, intuitively

- **Pixel size.** 1 arcminute = `1/60°`. So one pixel spans `0.01666…°` in
  both latitude and longitude — about **1.85 km × 1.85 km** at the equator.
- **Grid extent.** The whole globe is tiled by `Nj × Ni = 10 800 × 21 600 =
~233 million` pixels.
- **Pixel-is-area, not pixel-is-point.** Each pixel covers a 1×1 arcmin
  square on the ground. By convention we describe a pixel by its **centre**:
  the cell at column `i`, row `j` has centre

  $$
  \text{lon}_i = -180\degree + (i + \tfrac{1}{2}) / 60, \qquad
  \text{lat}_j = +90\degree - (j + \tfrac{1}{2}) / 60.
  $$

  So the first column's centre is `-179.99166…°`, the last is
  `+179.99166…°`; latitudes go top-down from `+89.99166…°` to
  `-89.99166…°`. This matches the grid definition encoded in
  `Globe_flood_area_*.grb` (verified via `eccodes`; see
  [Verifying alignment](#verifying-alignment)).

- **Why "snapping" matters.** A user-supplied AOI bbox almost never lines
  up with this grid. If we just resample to the AOI bounds, our output
  pixel centres land at fractional offsets like `-0.85166…°` instead of
  the canonical `-0.85833…°` — close, but **not stackable** with the
  global product. Snapping fixes this by extending the AOI outward to the
  nearest cell edges of the global grid before resampling.
- **Result.** After snapping, our AOI window is a contiguous slice of the
  global grid: `globe.isel(lat=slice(j0, j1), lon=slice(i0, i1))` and our
  harmonised raster cover the **same pixels**, byte-for-byte. Cross-product
  overlay (VIIRS vs `Globe_flood_area`, MERIT topography, etc.) becomes a
  trivial array subset, no resampling required.

### How it is implemented

This is enabled by default on every `--harmonise` run; you do not need to
pass any flag. See [`HarmoniseConfig`](../../src/atlantis/config.py) for the
three knobs and [`Reprojector._snap_bounds_to_global_grid`](../../src/atlantis/harmoniser/reprojector.py)
for the snap maths.

| Config field             | Default      | Meaning                                                                          |
| ------------------------ | ------------ | -------------------------------------------------------------------------------- |
| `target_resolution`      | `1/60` (deg) | Pixel size; `0.0166666…°` = exactly 1 arcmin.                                    |
| `snap_to_global_grid`    | `True`       | Snap AOI bounds to the canonical grid before resampling. Set `False` to disable. |
| `global_grid_origin_lon` | `-180.0`     | Western edge anchor.                                                             |
| `global_grid_origin_lat` | `+90.0`      | Northern edge anchor.                                                            |

Override via env vars (`ATLANTIS_SNAP_TO_GLOBAL_GRID=false`,
`ATLANTIS_TARGET_RESOLUTION=…`) or programmatically through `HarmoniseConfig`.

### Snap algorithm in one paragraph

For an AOI `(west, south, east, north)` and resolution `r = 1/60°`, we
extend the bounds **outward** to the nearest multiple of `r` from the
origins:

```
west_snap  = -180 + floor(( west + 180) / r) * r
east_snap  = -180 +  ceil(( east + 180) / r) * r
north_snap = +90  - floor(( 90  - north) / r) * r
south_snap = +90  -  ceil(( 90  - south) / r) * r
```

(then clipped to `[-180, 180] × [-90, 90]`). The number of pixels is
`(east_snap − west_snap) / r` × `(north_snap − south_snap) / r`, all
integers; pixel centres are exactly `±(k + 0.5) / 60`.

### Verifying alignment

The notebook [`notebooks/drafts/verify_global_grid.ipynb`](../../notebooks/drafts/verify_global_grid.ipynb)
walks through the verification end-to-end:

1. Streams ~200 MiB (one full message) of `Globe_flood_area_202208.grb`
   from `s3://atlantis/` using `aws s3api get-object --range`, then reads
   its grid definition with the `eccodes` Python API. Confirms `Ni=21600`,
   `Nj=10800`, `di=dj=1/60°`. (Note: GRIB stores longitudes in `[0, 360)`,
   so its first lon reads as `180.008°`, which is the same pixel as
   `-179.992°` in the `±180°` convention we use.)
2. Numerically proves that `Reprojector._snap_bounds_to_global_grid`
   maps an off-grid AOI (`-0.86, 8.26, 1.99, 11.73`) to a contiguous
   window of the global grid: `lon[10748:10920], lat[4696:4905]`.
3. Runs the full `Harmoniser` on a synthetic dataset and verifies that
   every output pixel centre satisfies `(lon + 180) × 60 − 0.5 ∈ ℤ` and
   `(90 − lat) × 60 − 0.5 ∈ ℤ` (deviation `< 1e-12`).

### Quick check on any harmonised file

```python
import rasterio

RES = 1 / 60
LON0, LAT0 = -180.0, 90.0

with rasterio.open("…_viirs_harmonised.tif") as src:
    t = src.transform
    cx0 = t.a * 0.5 + t.c          # first pixel centre, lon
    cy0 = t.e * 0.5 + t.f          # first pixel centre, lat
    kx = (cx0 - LON0) / RES - 0.5  # must be an integer
    ky = (LAT0 - cy0) / RES - 0.5
    print(f"pixel size : {t.a:.12f} (= 1/60: {RES:.12f})")
    print(f"on-grid    : kx={kx:.3e}, ky={ky:.3e}  (≈0 means aligned)")
```

For a West Africa AOI fetched with the default settings this prints
`kx=10748.000000, ky=4696.000000` — the AOI is rows 4696–4904 and
columns 10748–10919 of the global grid.

## Tips

- **Multiple dates** — The date range is inclusive: `--start-date 2024-10-27 --end-date 2024-10-31` fetches five daily composites.
- **Large regions** — VIIRS tiles cover ~10°×10°. Large bboxes automatically trigger multi-tile mosaicing.
- **Cloud contamination** — Always check the quality mask. VIIRS is an optical sensor and cloud cover masks flood signal.
- **Re-run without re-fetching** — With `--no-stream`, raw tiles are cached in `raw/` and reused on subsequent runs.
- **Batch KuroSiwo runs** — Use `--no-keep-processed` to save ~100 MB of intermediate files per event.
- **Pre-build metadata** — Run `atlantis build-kurosiwo-metadata` once to speed up repeated `fetch-kurosiwo-viirs` calls.

## Further reading

- [Pipeline modes & flowchart](pipeline.md)
- [Python API reference](api.md)
- [Architecture & internals](internals.md)
- [Pipeline vision](../../src/README.md)
- `scripts/viirs_demo.py` — runnable end-to-end example
- `notebooks/drafts/kurosiwo_viirs_showcase_cli.ipynb` — interactive walkthrough
