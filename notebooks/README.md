# Flood Benchmarking Notebooks

> **Origin:** These notebooks were migrated from
> [gpbalsamo/ifs-floodbench][floodbench] into atlantis.

[floodbench]: https://github.com/gpbalsamo/ifs-floodbench

## Overview

Benchmarking flood events using Earth Observation datasets:

- **GFM (Sentinel-1)**
- **VIIRS (Suomi NPP, NOAA-20, NOAA-21)**

The objective is to support validation and accelerate
the development of flood monitoring capabilities.

## Environment Note

These notebooks were originally developed to run on **ECMWF's HPC** (Atos).
Data paths (e.g. `/perm/pad/flood_cases/`) and environment variables (e.g. `$SCRATCH`)
are HPC-specific — update them for local use.

To install all notebook dependencies via pip/uv:

```bash
uv sync --extra notebooks
```

A Conda environment file (`gfm_env.yaml`) is also included for reference.

---

## Notebooks

### 1. Flood Event Visualisation

Visualisation of flood events from Cama-Flood (CMF) model outputs (3 arcmin resolution).

- **Notebook:** `Rivers-Inundation-Forecast.ipynb`
- **Inputs:**
  - `flood_case` name
  - geographical `area`

---

### 2. Extraction of EO Flood Data

#### Sentinel-1 (GFM)

Extraction of flood extent from the
**Global Flood Monitoring (GFM)** system
(~20 m resolution).

- **Notebook:** `Extract_GFM_Inundation.ipynb`

#### VIIRS

Extraction of flood extent from **VIIRS** (~375 m resolution).

- **Notebook:** `Extract_VIIRS_inundation.ipynb`

- **Environment requirement:**
  A Conda environment is required for GFM processing:
  `gfm_env.yaml` is included in the Notebooks directory.

- **Inputs:**
  - same `flood_case` name
  - same `area` definition

---

### 3. Benchmarking Framework

Comparison of CaMa-Flood (CMF) model simulations against
EO observations. Note the benchmark considers the model
grid as the target to which interpolating observations.
This choice was necessary due to the large discrepancy
between model output and observations resolutions.

#### CMF vs GFM

- **Notebook:** `Bench_CMF_GFM_Inundation.ipynb`

#### CMF vs VIIRS

- **Notebook:** `Bench_CMF_VIIRS_Inundation.ipynb`

- **Inputs:**
  - consistent `flood_case` name across datasets

---

## Methodological Background

The general benchmarking framework is described in:

- [A benchmarking framework for flood models using EO data](https://agupubs.onlinelibrary.wiley.com/doi/full/10.1029/2024MS004379)

---

## Requirements

All dependencies are available via the `[notebooks]` optional extra in atlantis:

```bash
uv sync --extra notebooks
```

Key packages: xarray, rioxarray, numpy, matplotlib,
rasterio, cartopy, odc-stac, earthkit-data, geopandas,
metview. See the root `pyproject.toml` for the full list.

> **Note:** `metview` may require a Conda install
> or ECMWF system packages on some platforms.

---

## Usage Notes

- Ensure consistent **naming of flood cases** across all notebooks
- Use the same **geographical area definition** when comparing datasets
- Avoid committing large EO datasets to the repository

---

## Author

Gianpaolo Balsamo
Calum Baugh
Kenka Tazi
Andreas Grafberger

ECMWF

---

## Future Work

- Extension to additional EO datasets
- Improved harmonisation across spatial resolutions
- Integration with Earth System Model workflows

---
