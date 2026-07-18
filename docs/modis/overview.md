<!-- markdownlint-disable MD013 MD051 -->

# MODIS Flood Detection

**Satellite-based flood mapping at 250 m resolution from NASA LANCE/LAADS**

This document collects everything we know about the NASA MODIS flood product
family — the data sources, formats, tiling scheme, pixel encoding, and the
access paths that matter for the integrated [MODIS fetcher](api.md).
For the architectural breakdown, see [internals.md](internals.md);
for the CLI decision tree, see [pipeline.md](pipeline.md).

Read this page for the product overview and quick-start guidance. Then use the
companion pages for the exact surface you need:

- [pipeline.md](pipeline.md) for backend choices, flag combinations, and strategy flow
- [api.md](api.md) for Python usage and constructor parameters
- [internals.md](internals.md) for architecture, search flow, and implementation details
- [../README.md](../README.md) for the full docs index

## Quick start

```bash
export EARTHDATA_TOKEN="YOUR_TOKEN_HERE"

uv run atlantis fetch \
  --event Pakistan_2022 \
  --source modis \
  --bbox "66.0 22.0 72.0 31.0" \
  --start-date 2022-08-30 \
  --end-date 2022-09-01 \
  --modis-backend laads_hdf4 \
  --modis-composite F2 \
  --harmonise \
  --no-keep-processed
```

This downloads the historical LAADS HDF4 products for the 2022 Pakistan floods,
extracts the 2-day flood composite, and writes the harmonised 1-arcmin output.

Typical folder layout:

```
<output>/
  <event_id>/
    modis/
      raw/          # downloaded .tif or .hdf inputs
      processed/    # absent with --no-keep-processed
      plots/
        processed/    # with --plot
        harmonised/   # with --harmonise
      harmonised/   # with --harmonise
```

## Pixel encoding at a glance

The MODIS flood products are categorical. The full semantics and release
history are documented later in this page; the quick reference is:

| Code  | Meaning                   |
| ----- | ------------------------- |
| `0`   | No water                  |
| `1`   | Surface / reference water |
| `2`   | Recurring flood           |
| `3`   | Unusual flood             |
| `255` | Insufficient data         |

Atlantis exposes these as **layers** of two kinds (full catalogue: [layer reference](../layers.md), or run `atlantis list-layers --source modis`):

- **Native** — the selected MCDWD flood composite, passed through untouched as `raw` (`--no-classify`). The eleven ancillary count layers are catalogued but not loaded by the default pipeline.
- **Derived** — the registry-defined MODIS layers emitted with `--classify`. Their exact names, dtypes, resampling, and descriptions are maintained only in the canonical [layer reference](../layers.md#layers-modis-derived).

`flood_fraction` is therefore a **derived** layer, not a native MCDWD value.

## What is MODIS?

MODIS (Moderate Resolution Imaging Spectroradiometer) instruments fly on the
NASA **Terra** (since 1999) and **Aqua** (since 2002) satellites. Each MODIS
collects 36 spectral bands at 250 m (bands 1–2), 500 m (bands 3–7) and 1 km
(bands 8–36) nominal resolution. The flood algorithm uses the **250 m red
(band 1) and near-infrared (band 2)** reflectances, plus **band 7 (SWIR,
2.13 µm)** which is delivered at 500 m and pan-sharpened to 250 m. All three
are consumed from the [MOD09 Surface Reflectance product](https://lpdaac.usgs.gov/documents/925/MOD09_User_Guide_V61.pdf)
(`MOD09.NRT.061` for the NRT chain).

Both satellites are nearing end-of-life: fuel exhaustion has caused their
orbits to drift, with Terra equatorial overpasses moving from ~10:30 a.m. to
~9:30 a.m. and Aqua from ~1:30 p.m. to ~3:00 p.m. as of late 2025. The
[VIIRS flood product](../viirs/overview.md) (NOAA-20 + NOAA-21) was designed as their
successor.

## Flood product family

NASA's "Global Flood Product" line currently bundles five distinct datasets
that all share the same algorithm, tile grid, and pixel encoding. The
algorithm is described in the
[MODIS/VIIRS NRT Global Flood Products User Guide, Rev F (Dec 2025)](https://www.earthdata.nasa.gov/s3fs-public/2025-12/MCDWD_VCDWD_UserGuide_RevF.pdf).

| Short name         | Format        | Period         | Source        | DOI                                  | Notes                                                                          |
| ------------------ | ------------- | -------------- | ------------- | ------------------------------------ | ------------------------------------------------------------------------------ |
| `MCDWD_L3`         | HDF4 (`.hdf`) | 2003 – 2025    | LAADS DAAC    | `10.5067/MODIS/MCDWD_L3.061`         | **Reprocessed** historical archive, released April 2026. Standard inputs.      |
| `MCDWD_L3_NRT`     | HDF4 (`.hdf`) | 2021 – present | LANCE + LAADS | `10.5067/MODIS/MCDWD_L3_NRT.061`     | Near-real-time, expedited geolocation. Archived in LAADS from Jan 2026 onward. |
| `MCDWD_L3_F1_NRT`  | GeoTIFF       | NRT (~1 week)  | LANCE         | `10.5067/MODIS/MCDWD_L3_F1_NRT.061`  | 1-day composite, single layer per file.                                        |
| `MCDWD_L3_F1C_NRT` | GeoTIFF       | NRT (~1 week)  | LANCE         | `10.5067/MODIS/MCDWD_L3_F1C_NRT.061` | 1-day with cloud-shadow screening.                                             |
| `MCDWD_L3_F2_NRT`  | GeoTIFF       | NRT (~1 week)  | LANCE         | `10.5067/MODIS/MCDWD_L3_F2_NRT.061`  | 2-day max-composite (cloud suppression).                                       |
| `MCDWD_L3_F3_NRT`  | GeoTIFF       | NRT (~1 week)  | LANCE         | `10.5067/MODIS/MCDWD_L3_F3_NRT.061`  | 3-day max-composite.                                                           |
| `MWP` (legacy)     | GeoTIFF       | 2012 – 2022    | NASA NRT      | —                                    | Discontinued precursor (pixel-size and tile naming differ). Not recommended.   |

All current products are **Collection 6.1, Release 1.1**.

### Composites: how the four flood layers are built

Each daily HDF holds **four flood layers** — `Flood 1-Day 250m`,
`Flood 1-Day CS 250m` (cloud-shadow screened), `Flood 2-Day 250m`, and
`Flood 3-Day 250m` — alongside the eleven ancillary count layers from which
they are derived (see [§ HDF4 layer inventory](#hdf4-layer-inventory)).

The rule that turns water detections into a flood layer is intentionally
simple: a pixel is marked as water in the **N-Day** composite if it
accumulates **at least N** water detections across the previous `N` days of
clear-sky Terra+Aqua observations. The 2-day and 3-day variants therefore
**suppress cloud-shadow false-positives** (which rarely repeat at the same
pixel as clouds drift) at the cost of temporal sharpness. The 1-day product
has no such suppression and is contaminated by cloud shadows whenever clouds
are present; the 1-Day CS variant additionally applies the MOD09 cloud-shadow
mask to mitigate this. **F2 is the recommended default** for event-scale
flood mapping (see [ifs-floodbench Scripts/README.md](https://github.com/gpbalsamo/ifs-floodbench/blob/main/Scripts/README.md)).
The LANCE NRT publishes each composite as a separate one-band GeoTIFF
(`MCDWD_L3_F{1,1C,2,3}_NRT`).

### Water-detection algorithm (per-pixel, per-swath)

The algorithm runs on each MOD09 swath granule before any tile mosaicing
or temporal compositing. A pixel is flagged as water iff:

$$
\frac{B_2 + A}{B_1 + B} < C \quad \textbf{AND} \quad B_1 < D \quad \textbf{AND} \quad B_7 < E
$$

where $B_1, B_2, B_7$ are MOD09 reflectance values scaled by `10000`, and the
constants are inherited from the legacy DFO algorithm:

| Constant | Value    | Role                                               |
| -------- | -------- | -------------------------------------------------- |
| `A`      | `13.5`   | Numerator offset (band 2)                          |
| `B`      | `1081.1` | Denominator offset (band 1)                        |
| `C`      | `0.7`    | Band-ratio threshold                               |
| `D`      | `2027`   | Band-1 brightness ceiling (rejects bright land)    |
| `E`      | `675.7`  | Band-7 SWIR ceiling (rejects bright/snow surfaces) |

If bands 1 or 2 are saturated or NODATA the pixel is dropped as NODATA; if
only band 7 is missing the test is evaluated without the third clause. The
resulting per-swath water mask is then masked further by terrain-shadow,
cloud-shadow (1-Day CS only), and HAND topographic masks before
compositing — see [§ Shadow and topographic masking](#shadow-and-topographic-masking).
Water detections are then accumulated over 1, 2 or 3 days and thresholded
as described above. Flood vs surface water is resolved against the
[reference water mask](#surface-water-vs-flood-reference-water-mask) after
compositing, with `recurring flood` tagged via a separate monthly mask
([Recurring flood, Release 1.1](#recurring-flood-release-11-dec-2025)).

### Shadow and topographic masking

Four masks shape the final flood layers. Three remove false-positive water
detections (terrain shadow, HAND, cloud shadow); the fourth (insufficient
data) records where the algorithm was unable to make a determination.

#### Terrain-shadow mask (per-swath, before compositing)

Monthly precomputed shadow rasters generated from the **ASTER GDEM v2** at
nominal Terra/Aqua overpass geometries. For any given date the most
liberal mask (closest to the winter solstice) is applied. Removes
75–90 % of terrain-shadow false-positives in the 2-Day product. Currently
applied only to the **original 223 product tiles**; the **64 tiles added
at Release 1** (Apr 2024) rely solely on HAND for terrain-shadow
mitigation.

#### HAND mask (post-compositing, since Beta 2 / Jan 2023)

[Height Above Nearest Drainage](https://doi.org/10.1016/j.jhydrol.2011.03.051)
is a topographic feature that records the vertical distance from each pixel
to its nearest drainage channel. The MCDWD pipeline uses HAND to mask
pixels that are physically unlikely to flood at 250 m scale.

| Parameter                | Value                                                                                                     |
| ------------------------ | --------------------------------------------------------------------------------------------------------- |
| Source DEM               | **Copernicus GLO-90** (~90 m, 3 arc-second), via OpenTopography on AWS                                    |
| Drainage area            | ~48 km² (6 000 GLO-90 pixels) upstream-area threshold to define channels                                  |
| HAND threshold           | 30 m (binary mask)                                                                                        |
| Cleanup                  | Morphological dilation/erosion to remove pixelated noise                                                  |
| Reference-water override | MOD44W water (dilated by 1 px) is **subtracted** so known lakes are reportable inside HAND-masked terrain |
| Tooling                  | [PCRaster 4.3.3](https://pcraster.geo.uu.nl/)                                                             |

The HAND mask is applied **after** compositing: every pixel under the mask
is reassigned to `255` (insufficient data) regardless of detected water.
Results: in mountainous tiles HAND eliminates the vast majority of remaining
terrain- and cloud-shadow false-positives that survive the 2-Day or 3-Day
composite. The cost is that **new reservoirs in HAND-masked terrain will
show no water for several years**, until the rolling reference-water mask
(below) graduates them out of HAND. Custom downstream pipelines can
reconstruct an unmasked composite from the HDF count layers if this is
undesirable.

#### Cloud-shadow mask (1-Day CS only)

The MOD09 (or VJ109/VJ209 for VIIRS) State-QA `cloud_shadow` flag, resampled
from 1 km to 250 m to match the product grid. Applied only to the
`Flood_1Day_CS_250m` variant because the flag is imperfect and can erase
real water under thin cloud. The companion `Flood_1Day_250m` is published
without this mask so users can compare and pick the better answer for their
AOI.

#### Insufficient-data flag (`255`)

Laid down first, using MOD09 State-QA `cloud_state` to mark pixels reported
as anything other than "clear". Three subtleties affect downstream code:

1. **`255` is overwriteable by water.** Composited water detections are
   written on top of the `255` mask, so a pixel can be flagged as water
   through a thin cloud while its neighbours show as insufficient data — a
   common pattern around river edges under broken cloud cover.
2. **HAND overrides everything.** After water is written, the HAND mask
   restamps `255` over any HAND-masked pixel, regardless of upstream
   detections.
3. **`255` is not equivalent to "no flood".** Treating it as `0` in
   downstream binary masks systematically under-reports flood. The
   suggested Atlantis mapping (`exclusion_mask = (class == 255)`) preserves
   the distinction.

### Surface water vs flood (reference water mask)

Detected water is split into "surface water" (class `1`) and "flood" (class
`3`) by intersection with a reference water layer. The provenance of that
layer has changed across releases:

| Release       | Date      | Reference water layer                                                                            |
| ------------- | --------- | ------------------------------------------------------------------------------------------------ |
| Beta / Beta 2 | 2021–2023 | **MOD44W Collection 5** (Carroll et al. 2009; static, derived from 2000–2002 MODIS Terra + SRTM) |
| Release 1     | Apr 2024  | **Yearly MOD44W Collection 6.1** (Carroll et al. 2024), 5-year majority rule                     |
| Release 1.1   | Dec 2025  | Same as Release 1 + monthly **Recurring flood** mask (next section)                              |

#### How the yearly mask is built (Release 1 onward)

MOD44W C6.1 produces an annual binary water mask at 250 m. To smooth out
short-term anomalies (a particularly wet or dry year), MCDWD requires a
pixel to be **water in ≥ 3 of the 5 most recent annual MOD44W maps** before
it is treated as surface water. Two operational consequences:

- **New reservoirs are flagged as flood for ~3 years** before they
  graduate into the surface-water class.
- **Reference-water rotations happen every March 1.** Annual MOD44W layers
  are released in February; a 15 Mar 2025 product uses the 2020–2024 mask,
  while a 15 Feb 2025 product still uses the 2019–2023 mask.

Users with their own up-to-date surface-water inventory can re-classify
detected water as flood / non-flood downstream; the User Guide explicitly
encourages this.

### Recurring flood (Release 1.1, Dec 2025)

Release 1.1 introduced **monthly recurring-flood masks** that separate
seasonal flooding from event-driven flooding. The masks are derived from
the **22-year reprocessed MODIS archive (2003–2024)** and emitted with their
own class code (`2`) so users can trivially exclude or include them.

#### Algorithm (Clopper–Pearson confidence-bounded flood frequency)

For each 250 m pixel, the algorithm walks rolling **3-month windows**
centered on each calendar month (e.g. the January window spans Dec–Jan–Feb),
for each year `2003–2024`:

1. **Per-window counts.** From the daily 2-Day flood composites:
   - $K$ = `FloodCounts` (days flagged as flood, max ~90)
   - $N$ = `ValidCounts` (days with valid clear-sky observations)
2. **Sufficiency filter.** Drop windows with $N < 10$ (< 11 % of days valid)
   — too sparse to estimate flood frequency reliably.
3. **Clopper–Pearson lower bound (90 % one-sided).** For each remaining
   window compute

   $$
   q_L = p_L\!\left(K, N, \alpha = 0.10\right)
   $$

   where $q_L$ is the lower 90 % confidence bound on the true flood
   frequency given $(K, N)$, and convert to an _equivalent_ number of
   confidently-flooded days within a notional 90-day window:

   $$
   D_{\mathrm{cp}} = 90 \cdot q_L
   $$

4. **Year qualifies as a flood year if** $D_{\mathrm{cp}} \geq D$ where
   `D = 3` (chosen empirically over `D ∈ {3, 5, 7, 9}`). The Clopper–Pearson
   step is essential: it suppresses spurious detections in cloudy years
   where $K/N$ is small but noisy.
5. **Recurring flood if** the pixel qualifies in **≥ `Y` of the past
   `W` years**, with `Y = 7`, `W = 22` (so "flooded in roughly one third of
   the years on record"). A 3×3 majority filter (fill-only) cleans up
   isolated speckle.

The resulting 12 monthly masks ship as static lookups: at production time,
any detected-water pixel that matches the active month's recurring-flood
mask is emitted as class `2` instead of class `3`. There is currently no
yearly variation — future releases may re-derive the masks on a rolling
basis.

> Compatibility: in the **2021–2025 beta releases of MCDWD**, class `2` was
> reserved but never populated — every event-driven flood was emitted as
> class `3`. Code that needs to ingest both old and new files should treat
> `{2, 3}` as a single flood mask when reading pre-Release-1.1 archives.

## Pixel encoding (Release 1.1, Dec 2025)

Release 1.1 introduced the **unusual vs recurring** flood split. The encoding
applies to every flood layer (1-Day, 1-Day CS, 2-Day, 3-Day) in both MODIS
(MCDWD) and VIIRS (VCDWD) products.

| Code  | Meaning                   | Notes                                                                                      |
| ----- | ------------------------- | ------------------------------------------------------------------------------------------ |
| `0`   | No water                  | Land or out-of-detection                                                                   |
| `1`   | Surface / reference water | Permanent lakes, rivers, seas (from rolling 5-year MOD44W)                                 |
| `2`   | Recurring flood           | Pixel passes Clopper–Pearson recurring-flood test in current calendar month (Release 1.1+) |
| `3`   | Unusual flood             | **Current event flood** — the class of interest                                            |
| `255` | Insufficient data         | Cloud, cloud shadow, terrain shadow, no observation, **or HAND-masked**                    |

For most downstream work, treat **class `3`** as the binary flood mask, or
the union `{2, 3}` if you want to include seasonal flooding. See
[§ Recurring flood](#recurring-flood-release-11-dec-2025) for the algorithm
and [§ Insufficient-data flag](#insufficient-data-flag-255) for the
subtleties of `255`.

## Release history

| Release           | Date        | User Guide | `ALGORITHMPACKAGEVERSION` | Headline change                                                                                                                                                                                            |
| ----------------- | ----------- | ---------- | ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Beta              | 5 Mar 2021  | A          | `1.0`                     | Initial production, 12 HDF layers, MOD44W C5 reference water, 223 tiles.                                                                                                                                   |
| Beta (final)      | 25 Jun 2021 | B          | `1.0`                     | Qualitative evaluation completed.                                                                                                                                                                          |
| Beta 2            | 12 Jan 2023 | C          | `6.1.1 patch 12`          | **HAND mask added** — huge reduction in mountain false-positives.                                                                                                                                          |
| Release 1         | 16 Apr 2024 | D          | `6.1.0`                   | **Yearly MOD44W C6.1 reference water** (rolling 5-year), updated compositing thresholds, **287 tiles** (64 added), 15-layer HDF (Total Counts added).                                                      |
| Release 1.0 patch | 26 Nov 2024 | D.1        | `6.1.0`                   | Documentation patch only.                                                                                                                                                                                  |
| Release 1.1       | 11 Dec 2025 | F          | `6.1.1`                   | **Recurring flood class `2`** populated for the first time, derived from the 2003–2024 reprocessed archive (Clopper–Pearson algorithm). VIIRS Release 1.1 published in parallel as `VCDWD_L3_NRT` v 2.1.1. |
| LAADS archive     | 6 Apr 2026  | (n/a)      | (n/a)                     | **MCDWD_L3** (reprocessed 2003–2025) and **MCDWD_L3_NRT** (Jan 2026 onward) published on LAADS DAAC for long-term archive access.                                                                          |

Dates and version IDs are from the User Guide Rev F revision history and
the LAADS product description PDF.

## Comparison to the legacy MWP product

Before MCDWD, the same NASA team produced the **MODIS Water Product (MWP)**
from 2012 to 2022. MWP is the file prefix you'll still see referenced in
old flood-mapping tooling (`MWP_2022123_080E030N_3D3OT.tif`). It uses a
different pixel encoding and a different tile-naming convention; the User
Guide warns to expect both surface incompatibilities and small numerical
differences.

| Aspect           | MWP (legacy, 2012–2022)                                | MCDWD (LANCE, 2021–)                                                          |
| ---------------- | ------------------------------------------------------ | ----------------------------------------------------------------------------- |
| File format      | One GeoTIFF per composite                              | One HDF4 with 15 layers + GeoTIFFs                                            |
| Pixel encoding   | `0` insufficient, `1` no water, `2` surface, `3` flood | `0` no water, `1` surface, `2` recurring flood, `3` flood, `255` insufficient |
| Pixel size       | ~245 m (`0.0021968°`)                                  | ~232 m (`0.002083333°`)                                                       |
| Grid             | ~4552 × 4552, can drift by date                        | 4800 × 4800, fixed boundaries                                                 |
| Tile names       | `100E020N` (upper-left lat/lon)                        | `h28v07` (MODIS HV)                                                           |
| Recurring flood  | n/a                                                    | Yes (Release 1.1 onward)                                                      |
| 14-day composite | Yes                                                    | No (use Worldview to browse)                                                  |

MCDWD pixel boundaries are fixed across dates — a pixel at column `i`,
row `j` is the same patch of ground every day, so temporal stacking is a
trivial array operation. MWP grids drift by ±1 pixel between dates,
requiring per-date reprojection.

## Tile grid

MODIS flood tiles use a regular **10° × 10° linear lat/lon grid** with
`h ∈ [0, 35]` and `v ∈ [0, 17]` (max 36 × 18 = 648 tiles globally). The
set in production has grown across releases:

- **223 tiles** — original beta production (Mar 2021 through Beta 2 Jan 2023).
- **287 tiles** — since **Release 1 (Apr 2024)**: 64 additional land tiles
  added (e.g. small islands and coastal slivers previously excluded).
  Note that **terrain-shadow masks have not been generated for the 64 new
  tiles** — they rely solely on HAND for terrain-shadow mitigation.

The naming convention `hXXvYY` is the same one MODIS land products have
used since 2000 — see the
[MODLAND grid reference](https://modis-land.gsfc.nasa.gov/MODLAND_grid.html).

| Property    | Value                                                                       |
| ----------- | --------------------------------------------------------------------------- |
| Projection  | Geographic (`EPSG:4326`)                                                    |
| Pixel size  | `0.002083333333333°` exactly (`= 1/480°`)                                   |
| Tile size   | `4800 × 4800` pixels (~23 M pixels, ~23 MB uncompressed uint8)              |
| Tile extent | Exactly 10° × 10°                                                           |
| Grid origin | Fixed: pixel `(0, 0)` of tile `h00v00` is the upper-left at `(-180°, +90°)` |

Because the grid is geographic, the **on-the-ground pixel size varies
with latitude**: ~232 m at the equator, ~200 m at 30°, ~116 m at 60°.
This is a projection artefact, not a real resolution gain — the underlying
MODIS sensor still samples at ~250 m at nadir.

> **Datum.** The product is processed on a sphere of radius
> `6371007.181` m, but the HDF metadata field `SphereCode=12` is
> ambiguously reported as Clarke 1866 or WGS-84 by various GDAL builds.
> Differences are far smaller than one pixel; **treat the coordinates as
> WGS-84 / EPSG:4326** for all practical purposes.

Tile bounds (upper-left convention):

$$
\text{west} = -180\degree + 10h, \quad \text{north} = +90\degree - 10v
$$

```python
# How to translate an AOI bbox to MODIS h/v tiles
# (lifted verbatim from ifs-floodbench/Scripts/extract_modis_flood.py)
import numpy as np

def modis_ll_tiles_for_aoi(north, west, south, east):
    h_min = int(np.floor((west + 180.0) / 10.0))
    h_max = int(np.floor((east + 180.0) / 10.0))
    v_min = int(np.floor((90.0 - north) / 10.0))
    v_max = int(np.floor((90.0 - south) / 10.0))
    return [f"h{h:02d}v{v:02d}"
            for h in range(h_min, h_max + 1)
            for v in range(v_min, v_max + 1)]
```

## Data access

MODIS flood products live on two NASA-operated servers. **Both require a free
Earthdata Login bearer token** (no S3-style anonymous access exists). Set
`EARTHDATA_TOKEN` in your environment after registering at
<https://urs.earthdata.nasa.gov>.

### LAADS DAAC (historical + archived NRT)

- **Base URL** — `https://ladsweb.modaps.eosdis.nasa.gov/archive/allData/61/`
- **Date layout** — `<SHORTNAME>/<YYYY>/<DDD>/` where `DDD` is the
  3-digit day-of-year.
- **Listings** — HTML directory listings (parse `<a href>` links).
- **Coverage** — `MCDWD_L3` covers **2003 – 2025** (reprocessed). Pre-2003
  data (2000-055 – 2002-365) has not yet been reviewed for release; this
  is the **Terra-only era** before the Aqua launch in mid-2002, and NASA
  expects lower product quality there. `MCDWD_L3_NRT` is mirrored to
  LAADS from January 2026 onward.
- **No GeoTIFFs on LAADS.** LAADS distributes **HDF4 only**; the
  per-composite GeoTIFFs (`MCDWD_L3_F<X>_NRT`) are exclusive to the LANCE
  NRT servers.

### LANCE (live NRT, ~1-week window)

- **Primary** — `https://nrt3.modaps.eosdis.nasa.gov/archive/allData/61/`
- **Backup** — `https://nrt4.modaps.eosdis.nasa.gov/archive/allData/61/`
- **Window** — ~1 week (7–14 days) of recent NRT files; older files are
  evicted (the LAADS mirror is the long-term home from 2026 onward).
- **Subfolders** — `MCDWD_L3_NRT/` (HDF4 with all 15 layers) **and**
  one folder per single-composite GeoTIFF: `MCDWD_L3_F1_NRT/`,
  `MCDWD_L3_F1C_NRT/`, `MCDWD_L3_F2_NRT/`, `MCDWD_L3_F3_NRT/`.
- **Independent generation.** `nrt3` and `nrt4` are independent
  pipelines; if one is down, swap to the other.
- **Latency** — products are normally on the server within 3 hours of
  observation; appearance in Worldview adds another ~2 hours (~5 hours
  end-to-end).
- **Important quirk: NRT files are updated in place.** As new Terra/Aqua
  swath granules intersect a tile, the per-tile HDF (and its derived
  GeoTIFFs) is **regenerated** with the additional observations folded
  in. The filename does **not** change. The User Guide recommends polling
  with `If-Modified-Since` / comparing HTTP `Last-Modified` to detect
  these in-place updates. The production timestamp embedded in the
  filename (see below) is also useful for distinguishing reruns.

#### JSON listing API (the right way to discover files)

LANCE exposes a JSON listing endpoint that is far cheaper to scrape than
the HTML directory listings. It is the recommended path for any
automated pipeline.

```
https://nrt3.modaps.eosdis.nasa.gov/api/v2/content/details
  ?products=MCDWD_L3_NRT
  &archiveSets=61
  &temporalRanges=2024-235
```

Returns a JSON array of every available file for `MCDWD_L3_NRT` on
day-of-year 235 of 2024, including production timestamps. Two
operationally important uses:

1. **Detect in-place updates.** Cache the production timestamp from a
   previous call; if a tile's timestamp has changed, the file has been
   regenerated and should be re-fetched.
2. **Filter by AOI.** Walk the JSON, keep only entries whose `<TILE>`
   matches the `hXXvYY` set returned by `modis_ll_tiles_for_aoi(...)`,
   and stream those URLs through `/vsicurl/`. This is the MODIS analogue
   of the [VIIRS NoaaS3Backend listing flow](../viirs/internals.md#stage-2--materialise-inputs--viirsfetcherfetch).

The LAADS DAAC archive does **not** advertise an equivalent JSON API; for
LAADS the HTML listing under `archive/allData/61/MCDWD_L3/<YYYY>/<DDD>/`
remains the only programmatic listing route.

### Sibling NASA VIIRS products (for context only)

For completeness: NASA's MCDWD-equivalent VIIRS products live alongside
MCDWD on the same servers but under MODIS Collection `5200` rather than
`61`. **Atlantis currently uses the NOAA VFM VIIRS product instead** (see
[the VIIRS overview](../viirs/overview.md)); the NASA VIIRS line is listed here only so the family is
documented:

| Product             | Format       | Cadence               | Path                                                                                                                                                                                                                 |
| ------------------- | ------------ | --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `VCDWD_L3_NRT`      | HDF5 (`.h5`) | Daily                 | `nrt3.modaps.eosdis.nasa.gov/archive/allData/5200/VCDWD_L3_NRT/`                                                                                                                                                     |
| `VCDWDG_L3_NRT`     | HDF5 (`.h5`) | **Hourly cumulative** | `.../VCDWDG_L3_NRT/` — filenames carry an extra `<HOUR>` field (e.g. `VCDWDG_L3_NRT.A2025071.1100.h28v07.002.h5` was produced at 11:00 GMT). The 23:00 cycle is published as the day's final `VCDWD_L3_NRT` instead. |
| `VCDWD_L3_F<X>_NRT` | GeoTIFF      | Daily                 | `.../VCDWD_L3_F1_NRT/` etc. (same F1/F1C/F2/F3 layout as MODIS).                                                                                                                                                     |

The VIIRS NRT product uses the **same MODIS HV tile grid, the same pixel
encoding, and the same algorithm**; only the input source reflectance
(VJ109 / VJ209 from NOAA-20 / NOAA-21) differs. Adapting an MCDWD fetcher
to consume VCDWD would be mostly a path-and-extension change.

### Filename schema

```
<SHORTNAME>.A<YYYYDDD>.<TILE>.<COLLECTION>.<PRODTIMESTAMP>.<EXT>
```

Examples:

| Product            | Example filename                                        |
| ------------------ | ------------------------------------------------------- |
| `MCDWD_L3` (LAADS) | `MCDWD_L3.A2024235.h24v05.061.hdf`                      |
| `MCDWD_L3_NRT`     | `MCDWD_L3_NRT.A2022361.h19v06.061.2022362024142.hdf`    |
| `MCDWD_L3_F2_NRT`  | `MCDWD_L3_F2_NRT.A2026032.h09v05.061.2026032142200.tif` |

Fields:

- `A<YYYYDDD>` — acquisition year + day-of-year (`A2026032` =
  Feb 1, 2026).
- `<TILE>` — MODIS HV tile, e.g. `h09v05`.
- `<COLLECTION>` — `061` (Collection 6.1).
- `<PRODTIMESTAMP>` — production timestamp `YYYYDDDHHMMSS`. Present on
  NRT outputs (and rerun whenever the file is regenerated); the
  reprocessed `MCDWD_L3` archive also carries it.

### Authentication header

Every request needs a bearer token:

```bash
curl -H "Authorization: Bearer $EARTHDATA_TOKEN" \
  https://ladsweb.modaps.eosdis.nasa.gov/archive/allData/61/MCDWD_L3/2024/235/
```

GDAL knows how to pass it via `GDAL_HTTP_HEADERS`:

```bash
export GDAL_HTTP_HEADERS="Authorization: Bearer $EARTHDATA_TOKEN"
# Replace <PRODTIMESTAMP> with the value from the directory listing.
gdalinfo "/vsicurl/https://nrt3.modaps.eosdis.nasa.gov/archive/allData/61/MCDWD_L3_F2_NRT/2026/032/MCDWD_L3_F2_NRT.A2026032.h09v05.061.<PRODTIMESTAMP>.tif"
```

## File formats

### HDF4 (`MCDWD_L3` and `MCDWD_L3_NRT`)

The HDF4 files are **HDF-EOS2** (`.hdf`, version 2.20, based on HDF4)
containing a single Grid object named `Grid_Water_Composite`. **As of
Release 1 (Apr 2024), each file holds 15 raster subdatasets** — four flood
layers, three total-counts layers, four valid-counts layers, and four
water-counts layers. (Beta-era files held 12: the three Total Counts
layers were added at Release 1.) GDAL exposes them as `HDF4_EOS:EOS_GRID:...`
subdataset URIs.

The authoritative subdataset names (per User Guide Rev F, Dec 2025) use
**underscores, no spaces**, as e.g. `Flood_2Day_250m`. (The earliest beta
files used the form `"Flood 2-Day 250m"` with spaces; if you see that in
older code, it is the same layer.)

#### HDF4 layer inventory (Release 1 onward)

| #   | Subdataset (in `Grid_Water_Composite`) | Composite | Group        | Description                                                                            |
| --- | -------------------------------------- | --------- | ------------ | -------------------------------------------------------------------------------------- |
| 1   | **`FloodCS_1Day_250m`**                | F1C       | **Flood**    | 1-day flood, cloud-shadow mask applied.                                                |
| 2   | **`Flood_1Day_250m`**                  | F1        | **Flood**    | 1-day flood, no cloud-shadow mask.                                                     |
| 3   | **`Flood_2Day_250m`**                  | F2        | **Flood**    | 2-day composite.                                                                       |
| 4   | **`Flood_3Day_250m`**                  | F3        | **Flood**    | 3-day composite.                                                                       |
| 5   | `TotalCounts_1Day_250m`                | 1-Day     | Total counts | Number of _potential_ swath observations (no swath-gap, no bad data) for 1-day window. |
| 6   | `TotalCounts_2Day_250m`                | 2-Day     | Total counts | Same, 2-day window.                                                                    |
| 7   | `TotalCounts_3Day_250m`                | 3-Day     | Total counts | Same, 3-day window.                                                                    |
| 8   | `ValidCountsCS_1Day_250m`              | 1-Day CS  | Valid counts | Clear-sky observations with cloud-shadow mask, 1-day.                                  |
| 9   | `ValidCounts_1Day_250m`                | 1-Day     | Valid counts | Clear-sky observations, no cloud-shadow mask, 1-day.                                   |
| 10  | `ValidCounts_2Day_250m`                | 2-Day     | Valid counts | Clear-sky observations, 2-day window.                                                  |
| 11  | `ValidCounts_3Day_250m`                | 3-Day     | Valid counts | Clear-sky observations, 3-day window.                                                  |
| 12  | `WaterCountsCS_1Day_250m`              | 1-Day CS  | Water counts | Water detections (after terrain + cloud-shadow mask), 1-day.                           |
| 13  | `WaterCounts_1Day_250m`                | 1-Day     | Water counts | Water detections (terrain mask only), 1-day.                                           |
| 14  | `WaterCounts_2Day_250m`                | 2-Day     | Water counts | Water detections, 2-day window.                                                        |
| 15  | `WaterCounts_3Day_250m`                | 3-Day     | Water counts | Water detections, 3-day window.                                                        |

All layers are 4800 × 4800 uint8, fill = 255, valid range 0–3 for flood
layers and 0–254 for the count layers.

The **Counts layers are not just diagnostics** — they let users synthesise
custom composites at run-time, e.g. require
`WaterCounts_2Day_250m ≥ 3 AND ValidCounts_2Day_250m ≥ 6` for a stricter
2-day mask. They are also the only way to _reconstruct an unmasked
composite_ (without HAND), since HAND is applied to the flood layers but
not the counts.

#### Internal metadata worth reading

MCDWD (and VCDWD) files carry several useful metadata fields, accessible
via `gdalinfo` or `h4dump`:

| Field                     | Purpose                                                                                                                                                                                                                                                                                 |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `INPUTPOINTER`            | Lists the specific MOD09/MYD09 (or VJ109/VJ209) granules used as input. Filenames carry granule timestamps in `HHMM` format — useful for tracing back to a specific overpass. Only lists granules for the _current_ day; predecessor-day inputs require reading the previous day's HDF. |
| `ALGORITHMPACKAGEVERSION` | Production-code version (`6.1.0` at Release 1, `6.1.1` at Release 1.1). For the VIIRS sibling, the equivalent field is `AlgorithmVersion`.                                                                                                                                              |
| `SphereCode`              | `12` (see [datum note](#tile-grid)).                                                                                                                                                                                                                                                    |

#### GDAL access pattern

List subdatasets:

```bash
gdalinfo MCDWD_L3.A2024235.h24v05.061.hdf
# yields lines like:
# SUBDATASET_3_NAME=HDF4_EOS:EOS_GRID:"MCDWD_L3.A2024235.h24v05.061.hdf":Grid_Water_Composite:Flood_2Day_250m
```

Extract the 2-Day flood layer to a compressed GeoTIFF:

```bash
gdal_translate \
  HDF4_EOS:EOS_GRID:MCDWD_L3.A2024235.h24v05.061.hdf:Grid_Water_Composite:Flood_2Day_250m \
  flood_2day_h24v05.tif \
  -co COMPRESS=DEFLATE -co PREDICTOR=2
```

No shell quoting is needed for current Release 1 layer names (no spaces).
For pre-Release-1 archives that still use space-and-dash names (e.g.
`"Flood 2-Day 250m"`), wrap the whole subdataset URI in single quotes.

#### Three caveats for HDF4 ingestion

1. **Georeferencing is reliable but worth verifying.** HDF-EOS2 stores
   the projection and grid in a Grid metadata block; GDAL parses it
   correctly into an `Affine`. Older tooling that ignores HDF-EOS metadata
   may report "not georeferenced", in which case bounds must be
   reconstructed from the `hXXvYY` tile index — see
   `tile_bounds_from_filename()` in
   [`ifs-floodbench/Scripts/extract_modis_flood.py`](https://github.com/gpbalsamo/ifs-floodbench/blob/main/Scripts/extract_modis_flood.py).
2. **HDF4 streaming via `/vsicurl/` is limited.** Unlike Cloud-Optimized
   GeoTIFFs, HDF4 has no internal chunked layout suited to HTTP range
   reads. Practical pipelines should either (a) fully download the
   `.hdf` then `gdal_translate` the requested subdataset, or (b) prefer
   the LANCE single-composite GeoTIFFs for NRT work
   (`MCDWD_L3_F{1,1C,2,3}_NRT`), which **do** stream well.
3. **GDAL must be built with HDF4 support.** Conda's `libgdal-hdf4`
   package provides this; the default `pip install rasterio` wheels
   don't.

### GeoTIFF (`MCDWD_L3_F{1,1C,2,3}_NRT`)

LANCE re-emits each of the four flood layers as a standalone single-band
GeoTIFF, derived directly from the HDF4 subdataset (the User Guide
describes the output of `gdal_translate`). Properties:

- **CRS** — EPSG:4326 (geographic lat/lon).
- **Dtype** — uint8.
- **Nodata** — 255.
- **Band count** — 1 (one composite per file).
- **Filename mapping** — `MCDWD_L3_F1_NRT` ↔ `Flood 1-Day 250m`,
  `MCDWD_L3_F1C_NRT` ↔ `Flood 1-Day CS 250m`,
  `MCDWD_L3_F2_NRT` ↔ `Flood 2-Day 250m`,
  `MCDWD_L3_F3_NRT` ↔ `Flood 3-Day 250m`. (The User Guide Rev A sometimes
  writes these as `MCDWD_F1_L3_NRT` etc.; the LANCE-deployed folders use
  the `MCDWD_L3_F<X>_NRT` form.)
- **Streamability** — files are flat one-band TIFFs without internal
  overviews, but small enough (a few MB per tile) that GDAL range reads
  work fine through `/vsicurl/`.

## Streaming strategy for Atlantis

Mapping the VIIRS pipeline onto MODIS, the obvious recipe is:

1. **Use LANCE GeoTIFFs (`MCDWD_L3_F2_NRT` etc.) wherever possible.** They
   stream cleanly via `/vsicurl/`, one band per file, just like the NOAA
   VIIRS S3 tiles. Atlantis already has the `merge → mask` plumbing in
   [`ViirsRasterProcessor`](../../src/atlantis/fetchers/viirs/processor.py)
   that can be reused almost verbatim.
2. **Use LAADS HDF4 (`MCDWD_L3`, `MCDWD_L3_NRT`) for any non-NRT date.**
   These can't be streamed efficiently, so the pipeline must download the
   `.hdf`, then `gdal_translate` the requested `Flood_*Day_250m`
   subdataset to a temporary GeoTIFF before mosaic-and-clip. This mirrors
   the `prepare_input_geotiffs()` helper in
   [ifs-floodbench/Scripts/extract_modis_flood.py](https://github.com/gpbalsamo/ifs-floodbench/blob/main/Scripts/extract_modis_flood.py).
3. **Re-use the existing AOI snapping / 1-arcmin harmonisation grid**
   ([VIIRS canonical 1-arcmin global grid](../viirs/overview.md#canonical-1-arcmin-global-grid)).
   MODIS is at ~250 m, finer than VIIRS' 375 m and finer than the 1-arcmin
   target, so `average` resampling is appropriate for `flood_fraction`-
   style downstreams. Note that the MODIS product is **categorical**
   (classes 0/1/2/3/255), so the resampler choice matters: see below.

### Resampling categorical MODIS pixels

Atlantis' VIIRS pipeline derives a continuous `flood_fraction` from raw
VIIRS codes 101–200 (water fraction in percent) and resamples with
`average`. **MODIS does not provide a water fraction** — the four classes
are mutually exclusive. Two reasonable adaptations:

| Strategy            | Resampler | Output meaning                                               |
| ------------------- | --------- | ------------------------------------------------------------ |
| Binarise → fraction | `average` | `flood_fraction` = fraction of 250 m sub-pixels with class 3 |
| Preserve classes    | `mode`    | Majority class per 1-arcmin pixel (integer 0/1/2/3/255)      |

The binarised path is the most directly comparable to VIIRS output. The
recurring-flood class (2) can be folded in as a separate mask layer
alongside VIIRS `reference_water`.

### Suggested layer mapping

This mapping is **implemented**: MODIS exposes the same sensor-agnostic
**derived** layers VIIRS does (so the harmoniser and ECMWF benchmarking notebooks,
[notebooks/ecmwf/Bench_CMF_VIIRS_Inundation.ipynb](../../notebooks/ecmwf/Bench_CMF_VIIRS_Inundation.ipynb),
stay source-agnostic). The `raw` row is the **native** passthrough; the count
layers are catalogued native layers that are not loaded by default.

| MODIS variable    | Kind    | Derivation                             | Equivalent VIIRS variable |
| ----------------- | ------- | -------------------------------------- | ------------------------- |
| `water_fraction`  | derived | `class ∈ {1,2,3}` aggregated to target | `water_fraction`          |
| `flood_fraction`  | derived | `class == 3` aggregated to target      | `flood_fraction`          |
| `exclusion_mask`  | derived | `class == 255`                         | `exclusion_mask`          |
| `reference_water` | derived | `class ∈ {1,2}`                        | `reference_water`         |
| `recurring_flood` | derived | `class == 2`                           | _(none — MODIS-only)_     |
| `raw`             | native  | original uint8 composite codes         | `raw`                     |

## NRT vs reprocessed: which one to use

The `MCDWD_L3` and `MCDWD_L3_NRT` collections share an algorithm but differ
in upstream inputs. Two production differences propagate into the flood
product, neither one large but worth knowing about for benchmarking work:

1. **Geolocation inputs.** NRT processing uses expedited
   attitude/ephemeris (Terra: pulled from L0 telemetry rather than
   flight-dynamics products; Aqua: predicted ephemeris). Reprocessed
   data uses standard inputs. Sub-pixel geolocation offsets in NRT can
   produce occasional flood false-positives or false-negatives,
   especially along high-contrast water/land edges.
2. **MOD09 atmospheric correction.** NRT MOD09 uses expedited ancillary
   atmospheric inputs and (when same-day inputs are unavailable) earlier
   ancillaries. The reprocessed chain uses standard MOD09. Differences
   show up as subtle shifts in detected water extent and in the spatial
   pattern of `255` ("insufficient data") pixels.

| Use case                                  | Recommended product                                          |
| ----------------------------------------- | ------------------------------------------------------------ |
| Operational monitoring, rapid response    | `MCDWD_L3_NRT` (LANCE)                                       |
| Historical mapping, validation, ML labels | `MCDWD_L3` (LAADS, 2003–2025)                                |
| Both — long timeline ending today         | `MCDWD_L3` for ≤ 2025-12-31, `MCDWD_L3_NRT` for ≥ 2026-01-01 |

The two collections **share the same tile grid, file format, layer
names, and pixel encoding**, so swapping between them is purely a matter
of picking the right LAADS or LANCE prefix at fetch time.

## Known caveats and failure modes

The User Guide is unusually frank about where the product fails. The
following are documented as expected behaviours — not bugs — and any
sensible benchmarking against MCDWD has to account for them.

| Failure mode                             | Why it happens                                                                                                                                                                     | Mitigation                                                                                                                    |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| **Cloud-shadow false-positives**         | Cloud shadows are spectrally near-identical to water in MOD09 bands.                                                                                                               | Use 2-Day or 3-Day composites; avoid 1-Day (no CS) under broken cloud.                                                        |
| **Terrain-shadow false-positives**       | Terrain shadows recur at the same pixel between Terra/Aqua passes.                                                                                                                 | Pre-applied monthly ASTER-GDEM masks remove 75–90 %; residuals concentrate in winter, high-latitude, mountainous tiles.       |
| **High-latitude over-reporting**         | Above 50° lat, multi-swath overlap inflates observation counts and propagates more cloud-shadow false-positives.                                                                   | Be skeptical of small flood signals at >50° lat; cross-check with Worldview reflectance.                                      |
| **Missed flooding under canopy / urban** | Tree cover and rooftops mask the water signal at 250 m.                                                                                                                            | Combine with SAR products in dense vegetation / urban areas.                                                                  |
| **Flash floods / mountain floods**       | Sub-pixel events not captured by 250 m grid; transient events miss the overpass window.                                                                                            | Treat MCDWD as floor-bound for events of this scale.                                                                          |
| **Sub-pixel water bodies**               | Anything narrower than ~one pixel is below the detection limit.                                                                                                                    | Expect under-reporting on rivers narrower than ~250 m; use Sentinel-1 / S2 for fine detail.                                   |
| **Volcanic dark surfaces**               | Recent lava flows have water-like spectral signatures.                                                                                                                             | Local masks (Hawaii, Maui already masked; Idaho Craters of the Moon, Iceland, Kamchatka not).                                 |
| **Sun-glint**                            | Specular reflections off water can fail the band-7 ceiling.                                                                                                                        | Dedicated glint correction is on the algorithm roadmap; until then, skip glint-prone tiles.                                   |
| **Outdated reference water**             | Pre-Release 1 used MOD44W C5 (2009); since Release 1 the rolling 5-year MOD44W C6.1 catches new reservoirs after ~3 years.                                                         | Override with a fresher mask if available; remember new reservoirs are flagged as flood until they graduate.                  |
| **Snow-melt over agriculture**           | Spring snowmelt creates shallow ponding on bare fields that the algorithm correctly flags as unusual water. The User Guide reports this for the US Northern Plains and Kazakhstan. | Cross-check against snow-cover layers and seasonality; treat early-spring "flood" claims in agricultural belts with caution.  |
| **Pre-2003 era**                         | Reprocessing for 2000-055 to 2002-365 is held back; this is the **Terra-only** period before Aqua launched.                                                                        | Use only after NASA releases an evaluation report for the pre-2003 archive.                                                   |
| **HAND-masked new reservoirs**           | A reservoir built inside a HAND-masked basin will report no water until the rolling MOD44W reference catches up (~3 years).                                                        | Reconstruct an unmasked composite from the `WaterCounts_*` and `ValidCounts_*` HDF layers if a known new reservoir is hidden. |

## Comparison to VIIRS

The NASA VIIRS NRT product (`VCDWD_L3_NRT`, see
[the VIIRS overview](../viirs/overview.md)) is intentionally a drop-in replacement for MODIS,
but Atlantis currently uses the **NOAA VFM** VIIRS product instead. The two
are different:

| Aspect            | MODIS MCDWD             | NASA VIIRS VCDWD           | NOAA VIIRS VFM (Atlantis default) |
| ----------------- | ----------------------- | -------------------------- | --------------------------------- |
| Sensors           | Terra + Aqua MODIS      | NOAA-20 + NOAA-21 VIIRS    | Suomi-NPP + NOAA-20 VIIRS         |
| Native resolution | 250 m                   | 375 m (resampled to 250 m) | 375 m                             |
| Pixel encoding    | 0/1/2/3/255 categorical | 0/1/2/3/255 categorical    | 0–200 (water-fraction codes)      |
| Tile grid         | MODIS `hXXvYY` 10°×10°  | MODIS `hXXvYY` 10°×10°     | VFM `GLB001`–`GLB145` 10°×10°     |
| File format       | HDF4 / GeoTIFF          | HDF5 / GeoTIFF             | GeoTIFF                           |
| Distribution      | LAADS + LANCE (auth.)   | LAADS + LANCE (auth.)      | NOAA S3 (anonymous)               |
| Streamable        | GeoTIFFs only           | GeoTIFFs only              | Yes (full pipeline)               |
| Coverage start    | 2003 (reprocessed)      | 2025-04-15                 | 2012 (with 2021–2022 gap)         |
| Recurring/unusual | Yes (since Dec 2025)    | Yes (since Dec 2025)       | No                                |

The MODIS product is the **only one with a deep historical archive**
(2003 onward) and the **only one with a recurring-flood mask** that we
have not yet integrated. Adding a MODIS fetcher therefore fills two gaps:
multi-decade benchmarking, and seasonal-flood awareness.

## Reference implementation

A complete (download-and-process, non-streaming) MODIS workflow already
exists in the sibling repository
[ifs-floodbench](https://github.com/gpbalsamo/ifs-floodbench):

- [`Scripts/extract_modis_flood.py`](https://github.com/gpbalsamo/ifs-floodbench/blob/main/Scripts/extract_modis_flood.py)
  — single-event LAADS or LANCE download + HDF4 → GeoTIFF + mosaic + clip.
- [`Scripts/modis_flood_events.py`](https://github.com/gpbalsamo/ifs-floodbench/blob/main/Scripts/modis_flood_events.py)
  — batch over a KuroSiwo-style CSV catalogue.
- [`Scripts/estimate_modis_peak_dates.py`](https://github.com/gpbalsamo/ifs-floodbench/blob/main/Scripts/estimate_modis_peak_dates.py)
  — equivalent to VIIRS' `strategy="peak"`: scans daily rasters and
  picks the date that maximises `flood_fraction × (1 − missing_fraction)`.
- [`Scripts/gapfill_modis_flood.py`](https://github.com/gpbalsamo/ifs-floodbench/blob/main/Scripts/gapfill_modis_flood.py)
  — temporal gap-filling of the categorical time series.
- [`Scripts/README.md`](https://github.com/gpbalsamo/ifs-floodbench/blob/main/Scripts/README.md)
  — usage docs for all of the above.

These scripts were the starting point for porting the workflow into the
Atlantis [`fetchers/modis/`](../../src/atlantis/fetchers/modis/) package.

## Further reading

- [Python API reference](api.md) — `MODISFetcher` usage with both
  backends.
- [Architecture & internals](internals.md) — module layout, HDF4
  extraction, classification rules, search diagnostics.
- [Pipeline modes & flowchart](pipeline.md) — CLI decision tree
  and strategy details.
- [VIIRS reference](../viirs/overview.md) — companion sensor with similar
  architecture.

## References

- [NRT Global Flood Products homepage](https://www.earthdata.nasa.gov/global-flood-product)
- [MODIS/VIIRS NRT Global Flood Products User Guide, Rev F (Dec 2025)](https://www.earthdata.nasa.gov/s3fs-public/2025-12/MCDWD_VCDWD_UserGuide_RevF.pdf) — the authoritative source for the algorithm, HAND mask, recurring-flood Clopper–Pearson math, 15-layer HDF inventory, JSON API, and release history.
- [MCDWD User Guide, Rev A (Mar 2021)](https://web.archive.org/web/2021*/MCDWD_UserGuide.pdf) — the original beta-release user guide is the historical source for the pre-Release-1 layer naming (with spaces); the corresponding text is no longer locally archived (Rev F supersedes it for all current details).
- [MODIS Historical Reprocessed Archive description (April 2026)](https://www.earthdata.nasa.gov/s3fs-public/2026-04/MCDWD_LAADS_Desc.pdf) — official LAADS description of `MCDWD_L3` and `MCDWD_L3_NRT`.
- [NASA Earthdata blog — 23-year archive release (April 10, 2026)](https://www.earthdata.nasa.gov/news/blog/nasa-enhances-global-flood-products-smarter-detection-flooding-release-23-year-archive)
- [LANCE NRT versus Standard Products](https://www.earthdata.nasa.gov/learn/earth-observation-data-basics/near-real-time-versus-standard-products) — background on the NRT/standard split that motivates the reprocessed archive.
- [MOD09 Surface Reflectance User Guide](https://lpdaac.usgs.gov/documents/925/MOD09_User_Guide_V61.pdf) — source product for the water-detection algorithm.
- MOD44W C6.1 User Guide & ATBD — documents the yearly MOD44W collection used as the rolling 5-year reference water mask since Release 1.
- [MOD44W Land–Water Mask C5 (Carroll et al. 2009)](https://doi.org/10.1080/17538940902951401) — the static mask used by Beta releases.
- [HAND model paper (Nobre et al. 2011)](https://doi.org/10.1016/j.jhydrol.2011.03.051) — the topographic feature underpinning the HAND mask.
- [Copernicus GLO-90 DEM via OpenTopography](https://portal.opentopography.org/raster?opentopoID=OTSDEM.032021.4326.3) — source DEM for HAND.
- [MCDWD_L3_NRT catalogue page (LAADS)](https://ladsweb.modaps.eosdis.nasa.gov/missions-and-measurements/products/MCDWD_L3_NRT/)
- [VCDWD_L3_NRT catalogue page (Earthdata, NASA VIIRS)](https://www.earthdata.nasa.gov/data/catalog/lancemodis-vcdwd-l3-nrt-2)
- [MODIS Land Team homepage](https://modis-land.gsfc.nasa.gov/) — includes the [MODLAND HV grid reference](https://modis-land.gsfc.nasa.gov/MODLAND_grid.html).
- [MODIS Ocean Color homepage](https://oceancolor.gsfc.nasa.gov/)
- [LAADS DAAC bulk download instructions](https://nrt3.modaps.eosdis.nasa.gov/help/downloads)
- [LANCE JSON listing API](https://nrt3.modaps.eosdis.nasa.gov/api/v2/content/details) — the recommended programmatic-discovery endpoint.
- [Earthdata Login (token registration)](https://urs.earthdata.nasa.gov/)
- [FLOOD Viewer web tool (compare MODIS / VIIRS / OPERA)](https://lance.modaps.eosdis.nasa.gov/flood)
- [Atlantis VIIRS reference docs](../viirs/overview.md)
