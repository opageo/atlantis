<div align="center">

# Project: Atlantis

<img src="docs/assets/logo.png" alt="Project Atlantis Logo" width="320">

</div>

ML-ready archive of satellite-derived flood inundation observations
(ECMWF Code for Earth 2026).

> **Getting started?** Read [src/README.md](src/README.md) for the current architecture guide, working VIIRS/KuroSiwo extraction commands, pipeline overview, module layout, and extension points.

[![Python versions][python-badge]][python-url]
[![Ruff][ruff-badge]][ruff-url]
[![Gitleaks status][gitleaks-badge]][gitleaks-url]

[python-badge]: https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue
[python-url]: https://github.com/opageo/atlantis
[ruff-badge]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json
[ruff-url]: https://github.com/astral-sh/ruff
[gitleaks-badge]: https://github.com/opageo/atlantis/actions/workflows/gitleaks.yml/badge.svg
[gitleaks-url]: https://github.com/opageo/atlantis/actions/workflows/gitleaks.yml

## Installation

```bash
uv sync
```

## CLI

- `atlantis fetch` — fetch VIIRS inundation data for an explicit bbox/date window
- `atlantis build-kurosiwo-metadata` — derive KuroSiwo metadata CSV from the GeoPackage catalogue
- `atlantis fetch-kurosiwo-viirs` — fetch VIIRS for KuroSiwo cases directly from the catalogue or a metadata CSV
- `atlantis harmonise` — resample fetched outputs to a uniform grid (1 arcmin) with normalisation
- `atlantis archive` — write Zarr archives (placeholder)
- `atlantis validate` — validate the archive (placeholder)

The exact working VIIRS and KuroSiwo extraction workflow is documented in [src/README.md](src/README.md).

## Notebooks

Flood benchmarking notebooks migrated from
[gpbalsamo/ifs-floodbench][floodbench] are in
[`notebooks/`](notebooks/).
They cover EO data extraction (GFM, VIIRS) and
benchmarking against ecLand-CaMa-Flood model outputs.

[floodbench]: https://github.com/gpbalsamo/ifs-floodbench

To install notebook dependencies:

```bash
uv sync --extra notebooks
```

See [`notebooks/README.md`](notebooks/README.md) for details.

## Download Kuro Siwo Dataset

The catalog of Kuro Siwo is stored in the git LFS of this repository, under `./assets/ks_catalogue.gpkg`, before you use it, make sure you have `git lfs` installed (if not install if with `git lfs install`) and the dataset is pulled, the first time you may need to execute:

```bash
git lfs pull
```
