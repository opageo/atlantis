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

## Using devcontainer as zero setup environment for Atlantis
In the project of Atlantis we are exploring using [devcontainers](https://containers.dev/) github's technology for offering a zero-setup containerized environment to be ready to go for running all the Atlantis features.

### Prerequisites of Devcontainers
1. You are using [vs-code](https://code.visualstudio.com/) as your development editor.
2. You have set your `~/.aws/config` and `~/.aws/credentials` files with the suggested configuration for using the Atlantis S3 Storage bucket and authenticating against the several datasets providers - request for the administrators aws keys and setup template.
3. You have created an EARTHDATA_TOKEN for using MODIS pipeline [how-to-setup-earthdata-token](#earthdata-token-guideline).
4. *Extra if you need to actively contribute: enable SSH-Agent forwarding to authenticate against github. Here is [how-to](#add-ssh-forwarding) if you are using already a remote-ssh connection to your VM

#### Earthdata Token guideline:
- Create an account at https://search.earthdata.nasa.gov/ 
- **Important note:** you need to add the organization/institute field for your token to be enabled.
- Generate a token by clicking to -> profile -> Generate_Token tab
- Make sure you expose the token to your environment variables as: `EARTHDATA_TOKEN=<TOKEN>`
  The simplest way is adding the token to the `.env` file in your workspace, which is automatically activated from vs-code. 
  **Be careful** and never commit an environment file exposing your token/credentials  

#### Add SSH forwarding
##### For your vscode-server - in case your already using a remote-ssh connection

In order to enable active authentication in your dev-container session, you'll need to forward the ssh-agent.
For linux systems, while conencted in a VM with remote-ssh vs-code server, create the `~/.vscode-server/server-env-setup` and add:
```bash
# Sourced by the VS Code Server on startup.
# Make the host's ssh-agent (started by ~/.bash_profile on login) visible
# to the VS Code Server process so the Dev Containers extension can forward it.

if [ -z "$SSH_AUTH_SOCK" ] && [ -f "$HOME/.ssh/ssh-agent" ]; then
    # Start a new agent if the recorded socket is stale or missing.
    if ! eval "$(cat "$HOME/.ssh/ssh-agent")" > /dev/null 2>&1 \
       || [ ! -S "$SSH_AUTH_SOCK" ]; then
        ssh-agent -s > "$HOME/.ssh/ssh-agent"
        eval "$(cat "$HOME/.ssh/ssh-agent")" > /dev/null
        ssh-add "$HOME/.ssh/id_ed25519" > /dev/null 2>&1
    fi
    export SSH_AUTH_SOCK SSH_AGENT_PID
fi
```
if you use your own ssh key instead of the default `$HOME/.ssh/id_ed25519` replace that accordingly.