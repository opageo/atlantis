# Setting up Atlantis with pixi

[Pixi](https://pixi.sh) provides a single-command setup that installs the
**system dependencies** — GDAL with HDF4 support and the packages built against
it (rasterio, geopandas, pyproj, shapely, pyarrow) — from conda-forge. No manual
compilation or system package management is needed.

`pyproject.toml` stays the **single source of truth for all PyPI dependencies**:
pixi installs `atlantis` itself (editable) with the `all` extra, so every
optional group from `pyproject.toml` (`batch`, `stac`, `viz`, `notebooks`, `ui`,
`hf`) is installed by default.

> **Disclaimer (Conda):** Atlantis does not officially support Conda-only
> workflows. We only provide `environment.yml` as a convenience export from
> `pixi.toml`.
> **Existing `uv` / `make` users:** the `pixi` path is an alternative. The
> `pyproject.toml` + `uv sync` workflow remains fully supported.

---

## 1. Install pixi

```bash
curl -fsSL https://pixi.sh/install.sh | bash
```

Restart your shell (or `source ~/.bashrc`) so the `pixi` command is on `PATH`.

---

## 2. Install the default environment

From the repository root:

```bash
pixi install
```

This resolves and installs:

- Python (3.12–3.14)
- GDAL **with the HDF4 driver** (`libgdal-hdf4` from conda-forge) — no manual
  build required — plus rasterio, geopandas, pyproj, shapely and pyarrow built
  against it
- The `atlantis` package itself and all its runtime dependencies **plus every
  extra** (`atlantis[all]`), from `pyproject.toml`
- Dev tools (pytest, ruff, pre-commit, …)

Verify the GDAL/HDF4 stack:

```bash
pixi run verify-gdal
# Expected: GDAL 3.x.x — HDF4 driver: OK
```

### Optional: export a Conda environment file from `pixi.toml`

If you still need Conda, generate `environment.yml` from the Pixi lockfile and
then create the Conda environment:

```bash
pixi workspace export conda-environment --environment default --name default --from-lock-file environment.yml
conda env create -f environment.yml
conda activate default
python -m pip install -e . --no-deps
```

---

## 3. Bootstrap credentials & data assets

```bash
pixi run setup
```

This runs `scripts/setup.py` inside the pixi environment. It will prompt for:

- **NASA Earthdata token** (needed for VIIRS, MODIS)
- **ECMWF S3 access/secret keys** (for GFM object store)

See [setup.md](setup.md) for full details on each credential.

---

## 4. Run the demos

All demos fetch Valencia 2024 flood data, harmonise to 1 arcmin, and produce
plots under `./data/Valencia_2024/`.

```bash
pixi run demo             # VIIRS — default (flood-classified)
pixi run demo-modis       # MODIS — requires EARTHDATA_TOKEN
pixi run demo-gfm         # Sentinel-1 GFM — anonymous public STAC
```

**Raw variants** (skip flood classification, keep original pixel codes):

```bash
pixi run demo-raw         # VIIRS raw
pixi run demo-modis-raw   # MODIS raw
pixi run demo-gfm-raw     # GFM raw
```

MODIS demos require a NASA Earthdata token (run `pixi run setup` first).
GFM doesn't set `--no-stream` or `--no-classify` — these flags are ignored for
SAR data.

---

## 5. Running other examples

Pre-built tasks exist for all event/source combinations:

```bash
pixi run example-harvey-viirs       # VIIRS: Hurricane Harvey 2017
pixi run example-bihar-gfm          # GFM: Bihar floods 2019
pixi run example-vamco-modis        # MODIS: Typhoon Vamco 2020
pixi run example-westafrica-gfm     # GFM: West Africa floods 2020
```

List all available tasks:

```bash
pixi task list
```

For custom fetch commands, use the module path with ``:

```bash
 python -m atlantis.cli --verbose fetch \
  --event Harvey_2017 --source viirs \
  --bbox "-97.27 28.24 -95.54 29.80" \
  --start-date 2017-08-28 --end-date 2017-08-31 \
  --strategy all --peak-window-days 2 --max-observations 3 --peak-priority balanced \
  --plot --harmonise --no-keep-processed \
  --output ./data/Harvey_2017
```

---

## 6. Named environments

Because the default environment already installs **all** extras, the named
environments are mostly **aliases of `default`**: they contain the same PyPI
packages and exist so you can target a purpose with `pixi run -e <env>` /
`pixi shell -e <env>`. They all share one solve group, so conda package versions
are identical across them.

| Environment | Same packages as `default`? | Extra on top                     | Activate with             |
| ----------- | --------------------------- | -------------------------------- | ------------------------- |
| `default`   | —                           | —                                | `pixi shell`              |
| `batch`     | yes                         | —                                | `pixi shell -e batch`     |
| `events`    | yes                         | — (per-event GFM backfill tasks) | `pixi shell -e events`    |
| `stac`      | yes                         | —                                | `pixi shell -e stac`      |
| `ui`        | yes                         | —                                | `pixi shell -e ui`        |
| `viz`       | yes                         | —                                | `pixi shell -e viz`       |
| `notebooks` | no                          | JupyterLab, metview¹ (conda)     | `pixi shell -e notebooks` |
| `docs`      | no                          | MkDocs Material, mkdocstrings    | `pixi run -e docs docs`   |

¹ `metview-python` is only available on `linux-64`. On macOS the `notebooks`
environment installs everything except the conda `metview-python` package.

Several tasks pin their environment with `default-environment` (for example the
GEOID-Flood / KuroSiwo archive tasks use `events`, and `merge-yearly-zarr` uses
`stac`), so `pixi run <task>` picks the right one automatically.

Run a task in a specific environment:

```bash
pixi run -e batch test
pixi run -e notebooks python notebooks/ecmwf/Extract_VIIRS_inundation.ipynb
```

> **Disk space:** every named environment is installed as its own copy under
> `.pixi/envs/`, even when it is identical to `default` (several GB each). Only
> run `pixi install -e <env>` (or `pixi run -e <env>`) for the ones you use, and
> remove the rest with `rm -rf .pixi/envs/<env>`.

To change which packages an environment gets, add a `[feature.<name>.*]` table in
`pixi.toml` and reference it in `[environments]`. Do not add PyPI packages there;
put them in `pyproject.toml` (see [section 8](#8-bumping-dependencies)).

---

## 7. Troubleshooting

### Cleaning up to free disk space

Pixi caches packages and environments which can grow to several GB. To reclaim
space:

```bash
# Remove unused packages from the global conda cache
pixi clean cache

# Remove ALL environments and reinstall (nuclear option)
rm -rf .pixi
pixi install
```

The global package cache lives at `~/.cache/rattler/`. The per-project
environment and lockfile cache lives in `.pixi/`.

### Regenerating the lockfile

If you add or bump a dependency in `pyproject.toml` or `pixi.toml`:

```bash
pixi lock
```

This re-solves and updates `pixi.lock`. Commit the updated lockfile.

If the solve fails with a message like _"affine==3.0.0 … may be causing a
conflict"_, a conda package was pinned by an older lockfile and conflicts with a
newer `pyproject.toml` bound. Refresh that package and lock again:

```bash
pixi update <package>
pixi lock
```

### Lockfile conflicts after rebase

```bash
rm pixi.lock
pixi install
```

### GDAL version mismatch with system install

The pixi environment is self-contained. If you also have a system GDAL, make
sure you are running commands via `pixi run` or inside `pixi shell` so the
conda-forge GDAL takes precedence.

### Platforms

The lockfile covers all major platforms `linux-64`, `osx-arm64`, `osx-64`, `win-64`.

---

## 8. Bumping dependencies

Edit PyPI dependencies **only in `pyproject.toml`** — `pixi.toml` picks them up
automatically. Then regenerate the lockfile:

```bash
pixi lock
git add pyproject.toml pixi.lock
git commit -m "chore: bump <package>"
```

Only touch `pixi.toml` for system packages (GDAL, HDF4, PROJ, GEOS and packages
compiled against them: `rasterio`, `geopandas`, `pyproj`, `shapely`, `pyarrow`,
and the `pandas < 3` bound that must match `pyproject.toml`). New optional
dependency groups added to `pyproject.toml` are picked up automatically as long
as they are included in the `all` extra. The `dev` and `docs` tools (and
`jupyterlab`) are the one exception: pixi cannot read PEP 735
`[dependency-groups]`, so they are listed unpinned in `pixi.toml`.

---

## See also

- [setup.md](setup.md) — credential and data-access setup (shared with `uv` path)
- [gdal-install.md](gdal-install.md) — manual GDAL build (only needed if **not** using pixi)
- [cli.md](cli.md) — full CLI reference with all flags and sensor options
- [development.md](development.md) — `uv` setup, devcontainers, testing and CI
- [../README.md](../README.md) — project overview and quick start
