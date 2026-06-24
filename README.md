<div align="center">

# Project: Atlantis

<img src="docs/assets/logo.png" alt="Project Atlantis Logo" width="320">

</div>

ML-ready archive of satellite-derived flood inundation observations
(ECMWF Code for Earth 2026).

> **Getting started?** Read [src/README.md](src/README.md) for the current architecture guide, working VIIRS/KuroSiwo extraction commands, pipeline overview, module layout, and extension points.

[![Python versions][python-badge]][python-url]
[![Ruff][ruff-badge]][ruff-url]
[![cov][cov-badge]][cov-url]
[![Gitleaks status][gitleaks-badge]][gitleaks-url]

[python-badge]: https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue
[python-url]: https://github.com/opageo/atlantis
[ruff-badge]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json
[ruff-url]: https://github.com/astral-sh/ruff
[cov-badge]: https://ECMWFCode4Earth.github.io/atlantis/badges/coverage.svg
[cov-url]: https://github.com/ECMWFCode4Earth/atlantis/actions
[gitleaks-badge]: https://github.com/opageo/atlantis/actions/workflows/gitleaks.yml/badge.svg
[gitleaks-url]: https://github.com/opageo/atlantis/actions/workflows/gitleaks.yml

## Quick Start

Three commands to go from clone to VIIRS flood data:

```bash
make setup   # install deps + restore data assets
make demo    # run the Valencia 2024 flood example
```

Or equivalently:

```bash
uv sync --extra geo
uv run atlantis setup
uv run atlantis demo
```

### Using pixi (recommended for newcomers)

[Pixi](https://pixi.sh) installs **all** dependencies — including GDAL with
HDF4 support — in a single command:

```bash
pixi install        # resolve & install everything from conda-forge
pixi run setup      # bootstrap credentials & data assets
pixi run demo       # run the Valencia 2024 flood example
```

See [docs/pixi-setup.md](docs/pixi-setup.md) for the full guide.

> **Architecture and sensor guides?** See [docs/README.md](docs/README.md)
> for the data-source documentation index and shared design notes.

## Installation

```bash
uv sync
```

## Credentials & data access

Most backends require a NASA Earthdata account and, for the MODIS LAADS HDF4
backend, a one-time browser authorization step. Run the setup script to be
guided through all of it:

```bash
uv run python scripts/setup.py
```

See [docs/setup.md](docs/setup.md) for a full description of each credential
(Earthdata token, LAADS Web pre-authorization, AWS profiles for GFM).

## CLI

- `atlantis setup` — bootstrap required data assets (VIIRS AOI grid, KuroSiwo catalogue)
- `atlantis demo` — run the Valencia 2024 flood example end-to-end
- `atlantis fetch` — fetch VIIRS inundation data for an explicit bbox/date window
- `atlantis build-kurosiwo-metadata` — derive KuroSiwo metadata CSV from the GeoPackage catalogue
- `atlantis fetch-kurosiwo-viirs` — fetch VIIRS for KuroSiwo cases directly from the catalogue or a metadata CSV
- `atlantis harmonise` — resample fetched outputs to a uniform grid (1 arcmin) with normalisation
- `atlantis archive` — write Zarr archives (placeholder)
- `atlantis validate` — validate the archive (placeholder)

  > **Recommended flags for new users:** The default `peak` strategy
  > fetches and processes all dates, then keeps only the peak-flood date in memory.
  > Add `--no-keep-processed` to skip writing intermediate 375 m files, or
  > `--strategy aggregate` to return a temporal mean/mode composite.
  > Use `--no-stream` to download tiles to disk, or `--no-classify` for raw pixel codes.
  > See [docs/viirs/overview.md](docs/viirs/overview.md) for details.
  >
  > The exact working VIIRS and KuroSiwo extraction workflow is documented
  > in [src/README.md](src/README.md).

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

## Testing Github actions/workflows locally

### Install nektos github extension

```bash
gh extension install https://github.com/nektos/gh-act
```

### Ensure you have docker daemon running

Install and run docker daemon in a cent-os rocky-linux system:

```bash
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo && sudo dnf install -y docker-ce docker-ce-cli containerd.io && sudo systemctl enable --now docker && sudo usermod -aG docker $USER && newgrp docker
```

### Run actos with

```bash
gh act <event-name>
```

default event is `push`

### Run specific workflow by job name

```bash
gh act -l #lists all job names
gh act -j <job-name>
```
