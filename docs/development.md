# Development Guide

Contributor-oriented documentation: `uv` setup, devcontainers, running tests,
CI triggers, and testing GitHub Actions locally.

> **User-focused onboarding?** See [pixi-setup.md](./pixi-setup.md) for the
> recommended `pixi`-based setup.

## `uv` setup (contributor workflow)

[uv](https://docs.astral.sh/uv/) is the primary toolchain for contributors.
Three commands to go from clone to VIIRS flood data:

```bash
uv sync --extra geo
uv run atlantis setup
uv run atlantis demo
```

Or with `make` wrappers:

```bash
make setup   # install deps + restore data assets
make demo    # run the Valencia 2024 flood example
```

## Running tests

```bash
uv run pytest -n auto tests/          # skip E2E tests (default)
uv run poe test                        # same, via task runner
```

### E2E Testing

End-to-end tests (marked with `@pytest.mark.e2e`) require network access and
AWS credentials. They are **skipped by default** in local runs.

```bash
uv run pytest tests/ -m e2e -v
```

### Triggering E2E tests in PRs

E2E tests are **required before merging to main** but don't run on every commit.
Trigger them on a PR by:

1. **Adding the `run-e2e` label** — the workflow runs immediately
2. **Commenting `/run-e2e`** — the comment-trigger workflow adds the label

After a successful run the label is automatically removed. Re-add it or
comment `/run-e2e` again after pushing new commits.

## Linting & formatting

```bash
uv run poe lint         # ruff check
uv run poe lint-fix     # ruff check --fix
uv run poe format-fix   # ruff format
uv run poe precommit    # pre-commit run --all-files
```

## Devcontainers (zero-setup environment)

A [devcontainer](https://containers.dev/) configuration provides a
containerized, ready-to-go environment for running all Atlantis features.

### Prerequisites

1. [VS Code](https://code.visualstudio.com/) with the
   [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)
2. `~/.aws/config` and `~/.aws/credentials` configured for the Atlantis S3
   bucket and dataset providers — request credentials from the administrators
3. An `EARTHDATA_TOKEN` for the MODIS pipeline (see below)
4. *(Contributors only)* SSH agent forwarding to authenticate against GitHub
   when already connected via Remote-SSH (see below)

### Earthdata Token

1. Create an account at <https://search.earthdata.nasa.gov/>
2. **Important:** add an organization/institute field for your token to be enabled
3. Generate a token: Profile → Generate Token
4. Export it as `EARTHDATA_TOKEN=<TOKEN>` in your environment
5. The simplest way: add the token to a `.env` file in your workspace
   (automatically activated by VS Code). **Never commit this file.**

### SSH agent forwarding (Remote-SSH users)

If you use VS Code Remote-SSH to connect to a VM, forward the SSH agent so
the devcontainer can authenticate against GitHub.

On the remote VM, create `~/.vscode-server/server-env-setup`:

```bash
# Sourced by the VS Code Server on startup.
# Make the host's ssh-agent visible to VS Code Server for devcontainer forwarding.

if [ -z "$SSH_AUTH_SOCK" ] && [ -f "$HOME/.ssh/ssh-agent" ]; then
    if ! eval "$(cat "$HOME/.ssh/ssh-agent")" > /dev/null 2>&1 \
       || [ ! -S "$SSH_AUTH_SOCK" ]; then
        ssh-agent -s > "$HOME/.ssh/ssh-agent"
        eval "$(cat "$HOME/.ssh/ssh-agent")" > /dev/null
        ssh-add "$HOME/.ssh/id_ed25519" > /dev/null 2>&1
    fi
    export SSH_AUTH_SOCK SSH_AGENT_PID
fi
```

Replace `$HOME/.ssh/id_ed25519` with your own key path if different.

## Testing GitHub Actions locally

Install and use [act](https://github.com/nektos/act) via the `gh` CLI:

```bash
gh extension install https://github.com/nektos/gh-act
```

Ensure Docker is running, then:

```bash
gh act              # default event: push
gh act -l           # list all job names
gh act -j <job-name>  # run a specific job
```

### Docker on Rocky Linux

```bash
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo && \
sudo dnf install -y docker-ce docker-ce-cli containerd.io && \
sudo systemctl enable --now docker && \
sudo usermod -aG docker $USER && \
newgrp docker
```

## Building

```bash
uv build          # build package
make clean        # clean build artifacts
```

## Documentation site

```bash
uv sync --group docs && uv run mkdocs serve   # serve locally at :8000
uv run mkdocs build --strict                  # build static site
```

## Notebooks

Flood benchmarking notebooks migrated from
[gpbalsamo/ifs-floodbench](https://github.com/gpbalsamo/ifs-floodbench) are in
[`notebooks/`](../notebooks/).

To open them:

```bash
pixi shell -e notebooks
jupyter lab
# or, with uv:
uv sync --extra notebooks
uv run jupyter lab
```

### KuroSiwo catalogue

The KuroSiwo draft notebooks
([`kurosiwo_eda.ipynb`](https://github.com/opageo/atlantis/blob/main/notebooks/drafts/kurosiwo_eda.ipynb),
[`kurosiwo_viirs_showcase_cli.ipynb`](https://github.com/opageo/atlantis/blob/main/notebooks/drafts/kurosiwo_viirs_showcase_cli.ipynb))
require the KuroSiwo catalogue (~500 MB):

```bash
uv run python scripts/download_kurosiwo.py
```
