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
