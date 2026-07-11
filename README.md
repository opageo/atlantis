<div align="center">

# Project: Atlantis

<img src="docs/assets/logo.png" alt="Project Atlantis Logo" width="320">

</div>

ML-ready archive of satellite-derived flood inundation observations
(ECMWF Code for Earth 2026).

> **New to Atlantis?** Start with the onboarding guide:
> [docs/pixi-setup.md](docs/pixi-setup.md) — single-command setup with GDAL + HDF4 out of the box.
>
> **Contributor?** See [docs/development.md](docs/development.md) for `uv` setup, devcontainers, testing and CI.

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

[Pixi](https://pixi.sh) installs **all** dependencies — including GDAL with
HDF4 support — from conda-forge in a single command. This is the recommended
path for all users:

```bash
pixi install       # resolve & install everything
pixi run setup     # bootstrap credentials & data assets
pixi run demo      # run the Valencia 2024 flood example
```

See [docs/pixi-setup.md](docs/pixi-setup.md) for the full guide.

> **`uv` users:** `uv` is also fully supported. See
> [docs/development.md](docs/development.md) for the contributor workflow,
> devcontainer setup, and CI instructions.

## Documentation

Browse the full documentation site locally (MkDocs + Material):

```bash
pixi run -e docs docs
```

Then open <http://localhost:8000>. Includes architecture guides, data-source
pipelines, CLI reference and batch-processing walkthroughs. For contributors
using `uv`:

```bash
uv sync --group docs && uv run mkdocs serve
```

## Credentials & data access

Most backends require a NASA Earthdata account. Run the setup script to be
guided through all of it:

```bash
pixi run setup
```

See [docs/setup.md](docs/setup.md) for a full description of each credential
(Earthdata token, LAADS Web pre-authorization, AWS profiles for GFM).

## CLI

The commands you'll use most often:

- `pixi run setup` — bootstrap required data assets and credentials
- `pixi run demo` — run the Valencia 2024 flood example end-to-end
- `pixi run example-harvey-viirs` — Hurricane Harvey (VIIRS)
- `pixi run example-bihar-gfm` — Bihar floods (Sentinel-1 GFM)

For custom fetch commands, run `python -m atlantis.cli fetch` with the
`PYTHONPATH=src` prefix (all pixi tasks do this automatically). Add
`--verbose` before the subcommand for debug logging.

See [docs/cli.md](docs/cli.md) for the full CLI reference, [CLI_Examples.md](CLI_Examples.md)
for task-oriented walkthroughs, and `pixi task list` to list all available tasks.

## Development

All contributor documentation — `uv` setup, devcontainers, running tests,
E2E workflow, CI triggers — is consolidated in
[docs/development.md](docs/development.md).
