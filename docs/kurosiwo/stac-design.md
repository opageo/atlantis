# KuroSiwo — STAC Catalog Design

> Reference document for the KuroSiwo STAC catalog architecture.
> Implemented in [`src/atlantis/stac_catalog.py`](../../src/atlantis/stac_catalog.py).

---

## 1. KuroSiwo On-Disk Layout

```
assets/kurosiwo/
└── {actid}/                          flood event (e.g. 1111002)
    ├── catalogue.gkpg                event-level GeoPackage (note: intentional .gkpg typo)
    ├── 00/                           aoiid = null → UNLABELED tiles
    │   └── {2chars}/                 first 2 chars of grid_id hash (sharding)
    │       └── {grid_id_hash}/
    │           ├── info.json
    │           ├── MS1_IVV_{actid}_NA_{date}.tif
    │           ├── MS1_IVH_{actid}_NA_{date}.tif
    │           ├── SL1_IVV_{actid}_NA_{date}.tif
    │           ├── SL1_IVH_{actid}_NA_{date}.tif
    │           ├── SL2_IVV_{actid}_NA_{date}.tif   (if slavecov ≥ 2)
    │           ├── SL2_IVH_{actid}_NA_{date}.tif
    │           └── MK0_MNA_{actid}_NA_{date}.tif
    └── {aoiid}/                      aoiid ≥ 1 → LABELED tiles (e.g. 01, 02 ...)
        └── {grid_id_hash}/           flat layout (fewer tiles per event)
            ├── info.json
            ├── MS1_IVV_{actid}_{aoiid}_{date}.tif
            ├── MS1_IVH_{actid}_{aoiid}_{date}.tif
            ├── SL{n}_IVV_{actid}_{aoiid}_{date}.tif
            ├── SL{n}_IVH_{actid}_{aoiid}_{date}.tif
            ├── MK0_MNA_{actid}_{aoiid}_{date}.tif   ← flood label raster
            ├── MK0_DEM_{actid}_{aoiid}_{date}.tif
            ├── MK0_MLU_{actid}_{aoiid}_{date}.tif
            └── MK0_SLOPE_{actid}_{aoiid}_{date}.tif
```

### Naming convention: `{PTYPE}{RANK}_{PNAME}_{actid}_{aoiid|NA}_{source_date}.tif`

| Token       | Values                                     | Meaning                                             |
| ----------- | ------------------------------------------ | --------------------------------------------------- |
| `PTYPE`     | `MS`, `SL`, `MK`                           | Master (flood-time), Slave (pre-flood), Mask        |
| `RANK`      | `1`, `2`, `0`                              | Scene rank (1=master/slave1, 2=slave2, 0=ancillary) |
| `PNAME`     | `IVV`, `IVH`, `MNA`, `DEM`, `MLU`, `SLOPE` | Band/product name                                   |
| `aoiid\|NA` | `01`, `02`, `NA`                           | Area-of-Interest ID; `NA` = no AoI (unlabeled)      |

### Directory sharding: `00/` vs `01/`

| Top-level dir      | `aoiid`     | `pflood` / `pwater`               | Extra datasets                | Dir layout               |
| ------------------ | ----------- | --------------------------------- | ----------------------------- | ------------------------ |
| `00/`              | `null`      | **both `null`** — no flood labels | SAR + MNA only                | `{2char-prefix}/{hash}/` |
| `01/` (and higher) | integer ≥ 1 | **populated**                     | SAR + MNA + DEM + MLU + SLOPE | flat `{hash}/`           |

The 2-level prefix sharding in `00/` mirrors git object storage and prevents directory size blow-up at full dataset scale (unlabeled tiles greatly outnumber labeled ones).

---

## 2. `info.json` Schema

Each tile directory contains an `info.json` with:

```jsonc
{
  "grid_id": "09dd5f42-e684-5c57-82d8-141078640c62", // UUID with hyphens
  "actid": 1111002,
  "flood_date": "2020-09-14 08:00:00", // flood event timestamp
  "geom": "POLYGON ((... EPSG:3857 coordinates ...))",
  "aoiid": 1, // null for unlabeled
  "revision": 1, // null for unlabeled
  "version": 1, // null for unlabeled
  "slavecov": 2, // number of pre-flood slave scenes
  "mastercov": 1, // number of flood-time master scenes (always 1)
  "gvalid": true, // geometric validity flag
  "pcovered": 49.3, // % of tile covered by valid SAR data
  "pwater": 0.0, // % of tile covered by permanent water (null if unlabeled)
  "pflood": 4.75, // % of tile covered by flood (null if unlabeled) ← ML label
  "sources": {
    "SL1": {
      "source_date": "2020-07-05",
      "s1_ids": ["..."],
      "master": false,
      "crank": 1,
    },
    "SL2": {
      "source_date": "2020-06-23",
      "s1_ids": ["..."],
      "master": false,
      "crank": 2,
    },
    "MS1": {
      "source_date": "2020-09-15",
      "s1_ids": ["..."],
      "master": true,
      "crank": 1,
    },
  },
  "datasets": {
    "MS1_IVV_1111002_01_20200915": {
      "ptype": "MS",
      "pname": "IVV",
      "master": true,
      "dtype": "float32",
      "nodata": 0.0,
      "source_date": "2020-09-15",
    },
    // ... one entry per .tif file
  },
}
```

> **Note on `s1_ids`**: These are legacy ESA SciHub product UUIDs (pre-June 2023).
> They do **not** map to any field in the current CDSE STAC API.
> To retrieve the source Sentinel-1 scene, use `source_date` + the tile bbox
> against the CDSE STAC endpoint (`sentinel-1-grd` collection). See `stac_api.py`.

---

## 3. STAC Catalog Hierarchy

```
Catalog: kurosiwo
├── Collection: kurosiwo-labeled         (aoiid ≥ 1 → flood-labeled)
│   └── Collection: kurosiwo-labeled-{actid}
│       └── Item: kurosiwo-{actid}-{grid_id}
│           ├── datetime    = flood_date
│           ├── geometry    = tile polygon (info.json "geom", EPSG:3857 → WGS84)
│           ├── bbox        = [minx, miny, maxx, maxy] WGS84
│           ├── properties  → see Section 4
│           └── assets      → see Section 5
│
└── Collection: kurosiwo-unlabeled       (aoiid = null → no flood labels)
    └── Collection: kurosiwo-unlabeled-{actid}
        └── Item: kurosiwo-{actid}-{grid_id}
            └── (same structure, fewer assets, mna role = "data")
```

### Design rationale for the labeled/unlabeled split

1. **`MNA` is semantically different** in each partition:
   - Labeled (`01/`): flood mask → `role: label` (ground truth for training)
   - Unlabeled (`00/`): water/nodata mask → `role: data` (quality flag)
2. **Labeled collection is self-contained** for supervised ML: every item has
   `pflood` (patch-level label), `MNA` (pixel-level label), and ancillary bands.
3. **Unlabeled collection** is useful for self-supervised pre-training or domain
   adaptation without accidental label leakage.

---

## 4. STAC Item Properties

| Property         | Type        | Source                    | Notes                                      |
| ---------------- | ----------- | ------------------------- | ------------------------------------------ |
| `datetime`       | datetime    | `info.json["flood_date"]` | Flood event date — primary temporal anchor |
| `start_datetime` | datetime    | `min(source_dates)`       | Earliest SAR acquisition date              |
| `end_datetime`   | datetime    | `max(source_dates)`       | Latest SAR acquisition date                |
| `ks:actid`       | int         | `info.json`               | Flood event ID                             |
| `ks:grid_id`     | str         | `info.json`               | Spatial tile UUID                          |
| `ks:flood_date`  | str         | `info.json`               | Flood event timestamp                      |
| `ks:pflood`      | float\|null | `info.json`               | % tile covered by flood (null = unlabeled) |
| `ks:pwater`      | float\|null | `info.json`               | % tile covered by permanent water          |
| `ks:pcovered`    | float       | `info.json`               | % tile covered by valid SAR data           |
| `ks:slavecov`    | int         | `info.json`               | Number of pre-flood slave scenes (1 or 2)  |
| `ks:mastercov`   | int         | `info.json`               | Number of flood-time master scenes         |
| `ks:gvalid`      | bool        | `info.json`               | Geometric validity flag                    |
| `ks:aoiid`       | int\|null   | `info.json`               | Area-of-Interest ID (null = unlabeled)     |

---

## 5. STAC Asset Roles

| `pname` | Asset key pattern               | Role (labeled) | Role (unlabeled) | dtype   |
| ------- | ------------------------------- | -------------- | ---------------- | ------- |
| `IVV`   | `ms1_ivv`, `sl1_ivv`, `sl2_ivv` | `data`         | `data`           | float32 |
| `IVH`   | `ms1_ivh`, `sl1_ivh`, `sl2_ivh` | `data`         | `data`           | float32 |
| `MNA`   | `mk0_mna`                       | **`label`**    | `data`           | uint8   |
| `DEM`   | `mk0_dem`                       | `auxiliary`    | — (absent)       | float32 |
| `MLU`   | `mk0_mlu`                       | `auxiliary`    | — (absent)       | uint8   |
| `SLOPE` | `mk0_slope`                     | `auxiliary`    | — (absent)       | float32 |

Asset key derivation: take the dataset name prefix up to `_{actid}_` and lowercase it.
Example: `MS1_IVH_1111002_01_20200915` → key `ms1_ivh`.

Each asset also carries extra fields:

- `ks:ptype`: `MS` / `SL` / `MK`
- `ks:pname`: `IVV` / `IVH` / `MNA` / `DEM` / `MLU` / `SLOPE`
- `ks:master`: bool — whether this is the flood-time scene
- `ks:source_date`: ISO date string of the SAR acquisition
- `nodata`: nodata value from `info.json`
- `data_type`: numpy dtype string

---

## 6. Source Sentinel-1 Scene Mapping

The `s1_ids` in `info.json` are **legacy SciHub UUIDs** and no longer resolve in CDSE.
The correct retrieval path is via STAC search using `source_date` + tile bbox:

```python
# stac_api.py — get_scenes_for_actid() + query_stac_for_scenes()
scenes = get_scenes_for_actid(actid=1111002)   # groups catalogue by source_date
items  = query_stac_for_scenes(scenes)          # queries CDSE STAC by bbox + datetime
```

Source scene → tile mapping:

- `source_date` in `info.json["sources"]` = SAR acquisition date → STAC `datetime`
- Tile bbox (EPSG:3857 → WGS84) → STAC `bbox` search

---

## 7. Usage

### Generate the PoC catalog (event 1111002)

```bash
python -m atlantis.stac_catalog --event 1111002
# Output: data/stac/catalog.json  (self-contained STAC tree)
```

### Generate catalog for all events

```bash
python -m atlantis.stac_catalog \
    --event 1111002 --event 1111003 \
    --root assets/kurosiwo \
    --output data/stac
```

### Load the catalog with pystac

```python
import pystac

cat = pystac.Catalog.from_file("data/stac/catalog.json")
labeled = cat.get_child("kurosiwo-labeled")
event   = labeled.get_child("kurosiwo-labeled-1111002")

for item in event.get_items():
    print(item.id, item.properties.get("ks:pflood"))
    flood_mask = item.assets["mk0_mna"].href
    master_vv  = item.assets["ms1_ivv"].href
```

### Browse with STAC Browser (optional)

```bash
# Serve locally and open in stac-browser
python -m http.server 8080 --directory data/stac
# Point stac-browser at http://localhost:8080/catalog.json
```

---

## 8. Extension path

| When                   | What                                                                                                                                                               |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Serving via API        | Ingest the JSON tree into `pgstac` + serve with `stac-fastapi`                                                                                                     |
| Formal label extension | Add `https://stac-extensions.github.io/label/v1.0.1/schema.json` to items in the labeled collection                                                                |
| SAR extension          | Add `https://stac-extensions.github.io/sar/v1.0.0/schema.json` and populate `sar:polarizations`, `sar:instrument_mode`, `sar:frequency_band` from the source scene |
| Cloud hosting          | Replace `href` absolute paths with `s3://` or `https://` URIs                                                                                                      |
