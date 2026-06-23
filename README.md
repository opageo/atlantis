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

> **Architecture and sensor guides?** See [docs/README.md](docs/README.md)
> for the data-source documentation index and shared design notes.

## Installation

```bash
uv sync
```

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
  > Add `--no-keep-processed` to skip writing intermediate files, or
  > `--strategy aggregate` to return a temporal mean/mode composite.
  > Use `--no-stream` to download tiles to disk, or `--no-classify` for raw pixel codes.
  > For GFM, `--harmonise` is enabled by default (re-encodes to uint8 for
  > cross-source stacking); `--no-stream` and `--no-classify` are ignored.
  > See [docs/viirs/overview.md](docs/viirs/overview.md) and
  > [docs/gfm/overview.md](docs/gfm/overview.md) for details.

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

### Get it from Git-LFS

The catalog of Kuro Siwo is stored in the git LFS of this repository, under `./assets/ks_catalogue.gpkg`, before you use it, make sure you have `git lfs` installed (if not install if with `git lfs install`) and the dataset is pulled, the first time you may need to execute:

```bash
git lfs pull
```

### Get it from S3 bucket

If you have access to our atlantis bucket (provided on premise to mentors and partners of the project) you can download kurosiwo related data from our s3://atlantis bucket, e.g for the catalog: `s3://atlantis/assets/ks/ks_catalogue.gpkg`

## E2E Testing

End-to-end tests (marked with `@pytest.mark.e2e`) require network access and AWS credentials to fetch data from STAC APIs and S3. These tests are **skipped by default** in local test runs due to pytest configuration in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
addopts = "-m 'not e2e'"
```

### Running E2E tests locally

To run E2E tests locally, you must:

1. Have AWS credentials configured
2. Explicitly include them with the `-m e2e` flag:

```bash
uv run pytest tests/ -m e2e -v
```

### Triggering E2E tests in PRs

E2E tests are **required before merging to main** but don't run on every commit. Trigger them manually on a PR by either:

1. **Adding the `run-e2e` label** — the `.github/workflows/e2e.yml` workflow runs immediately
2. **Commenting `/run-e2e`** — the `.github/workflows/run-e2e-from-comment.yml` workflow adds the label, which triggers the E2E workflow

After a successful E2E run, the `run-e2e` label is automatically removed. To re-run after pushing new commits, simply re-add the label or comment `/run-e2e` again.

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

### Run act

```bash
gh act <event-name>
```

default event is `push`

### Run specific workflow by job name

```bash
gh act -l #lists all job names
gh act -j <job-name>
```
