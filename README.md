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

The commands you'll use most often:

- `atlantis setup` — bootstrap required data assets and credentials
- `atlantis demo` — run the Valencia 2024 flood example end-to-end
- `atlantis fetch` — fetch raw inundation data (VIIRS / MODIS / GFM) for an explicit bbox + date window
- `atlantis harmonise` — resample fetched outputs to a uniform 1 arcmin grid with normalisation
- `atlantis list-sources` — list all registered data sources

Add `--verbose` (or `-v`) **before** the subcommand for debug logging,
e.g. `uv run atlantis --verbose fetch ...`.

> **Full reference:** See [docs/cli.md](docs/cli.md) for every command,
> every flag, defaults, and sensor-specific options. For task-oriented
> walkthroughs across real flood events see
> [CLI_Examples.md](CLI_Examples.md).
>
> **Recommended flags for new users:** the default `peak` strategy
> fetches and processes all dates, then keeps only the peak-flood date
> in memory. Add `--no-keep-processed` to skip writing intermediate
> files, or `--strategy aggregate` to return a temporal mean/mode
> composite. Use `--no-stream` to download tiles to disk, or
> `--no-classify` for raw pixel codes. For GFM, `--harmonise` is
> enabled by default (re-encodes to uint8 for cross-source stacking);
> `--no-stream` and `--no-classify` are ignored.
> See [docs/viirs/overview.md](docs/viirs/overview.md),
> [docs/gfm/overview.md](docs/gfm/overview.md), and
> [src/README.md](src/README.md) for details.

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

### KuroSiwo catalogue

The KuroSiwo draft notebooks
([`kurosiwo_eda.ipynb`](notebooks/drafts/kurosiwo_eda.ipynb),
[`kurosiwo_viirs_showcase_cli.ipynb`](notebooks/drafts/kurosiwo_viirs_showcase_cli.ipynb))
require the KuroSiwo catalogue (~500 MB). Download it on demand with:

```bash
uv run python scripts/download_kurosiwo.py
```

This places the file at `assets/ks_catalogue.gpkg`. Alternatively, fetch it
from the atlantis S3 bucket: `s3://atlantis/assets/ks/ks_catalogue.gpkg`.

## Development

Contributor docs (running tests, E2E workflow, testing GitHub Actions
locally) live in [docs/development.md](docs/development.md).
