# Development Guide

Developer-oriented documentation for contributors and maintainers:
running tests, triggering CI workflows, and testing GitHub Actions locally.

For user-facing CLI usage, see [cli.md](./cli.md). For architecture and
extension points, see [src/README.md](../src/README.md).

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

## Testing GitHub Actions/workflows locally

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

## Using devcontainer as zero setup environment for Atlantis
In the project of Atlantis we are exploring using [devcontainers](https://containers.dev/) github's technology for offering a zero-setup containerized environment to be ready to go for running all the Atlantis features.

### Prerequisites of Devcontainers
1. You are using [vs-code](https://code.visualstudio.com/) as your development editor.
2. You have installed the dev-containers extension (https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)
3. You have set your `~/.aws/config` and `~/.aws/credentials` files with the suggested configuration for using the Atlantis S3 Storage bucket and authenticating against the several datasets providers - request for the administrators aws keys and setup template.
4. You have created an EARTHDATA_TOKEN for using MODIS pipeline [how-to-setup-earthdata-token](#earthdata-token-guideline).
5. *Extra if you need to actively contribute: enable SSH-Agent forwarding to authenticate against github. Here is [how-to](#add-ssh-forwarding) if you are using already a remote-ssh connection to your VM

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
gh act -l #lists all job names
gh act -j <job-name>
```

## Using devcontainer as zero setup environment for Atlantis
In the project of Atlantis we are exploring using [devcontainers](https://containers.dev/) github's technology for offering a zero-setup containerized environment to be ready to go for running all the Atlantis features.

### Prerequisites of Devcontainers
1. You are using [vs-code](https://code.visualstudio.com/) as your development editor.
2. You have installed the dev-containers extension (https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)
3. You have set your `~/.aws/config` and `~/.aws/credentials` files with the suggested configuration for using the Atlantis S3 Storage bucket and authenticating against the several datasets providers - request for the administrators aws keys and setup template.
4. You have created an EARTHDATA_TOKEN for using MODIS pipeline [how-to-setup-earthdata-token](#earthdata-token-guideline).
5. *Extra if you need to actively contribute: enable SSH-Agent forwarding to authenticate against github. Here is [how-to](#add-ssh-forwarding) if you are using already a remote-ssh connection to your VM

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
