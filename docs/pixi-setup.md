# Setting up Atlantis with pixi

[Pixi](https://pixi.sh) provides a single-command setup that installs **all**
dependencies — including GDAL with HDF4 support — from conda-forge. No manual
compilation or system package management is needed.

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

- Python (3.11–3.14)
- All runtime + geo dependencies (rasterio, xarray, geopandas, pystac, …)
- GDAL **with the HDF4 driver** (`libgdal-hdf4` from conda-forge) — no manual
  build required
- Dev tools (pytest, ruff, pre-commit, …)
- The `atlantis` package itself (available via `PYTHONPATH=src`)

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

For custom fetch commands, use the module path with `PYTHONPATH=src`:

```bash
PYTHONPATH=src python -m atlantis.cli --verbose fetch \
  --event Harvey_2017 --source viirs \
  --bbox "-97.27 28.24 -95.54 29.80" \
  --start-date 2017-08-28 --end-date 2017-08-31 \
  --strategy all --peak-window-days 2 --max-observations 3 --peak-priority balanced \
  --plot --harmonise --no-keep-processed \
  --output ./data/Harvey_2017
```

---

## 6. Optional environments

Beyond the default (geo + dev), several opt-in environments are available:

| Environment | Adds                                         | Activate with             |
| ----------- | -------------------------------------------- | ------------------------- |
| `ml`        | PyTorch (CPU), NumPy, scikit-learn           | `pixi shell -e ml`        |
| `notebooks` | earthkit-data, cartopy, metview¹             | `pixi shell -e notebooks` |
| `batch`     | Dask distributed, bokeh dashboard, rio-cogeo | `pixi shell -e batch`     |
| `docs`      | MkDocs Material, mkdocstrings                | `pixi run -e docs docs`   |
| `viz`       | HoloViz dashboard stack (panel, hvplot, …)   | `pixi shell -e viz`       |

¹ `metview-python` is only available on `linux-64`. On macOS (osx-arm64) the
notebooks environment installs everything except metview.

Run a task in a specific environment:

```bash
pixi run -e ml test
pixi run -e notebooks python notebooks/ecmwf/Extract_VIIRS_inundation.ipynb
```

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

If you add or bump a dependency in `pixi.toml`:

```bash
pixi install --frozen=false
```

This re-solves and updates `pixi.lock`. Commit the updated lockfile.

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

The lockfile covers `linux-64` and `osx-arm64`. Windows and Intel macOS are
not currently supported via pixi (use the `uv sync` path instead).

---

## 8. Bumping dependencies

When a dependency version is updated in `pyproject.toml`, mirror the change in
`pixi.toml` and regenerate the lockfile:

```bash
# Edit pixi.toml to match the new version constraint
pixi install
git add pixi.toml pixi.lock
git commit -m "chore: bump <package> in pixi.toml"
```

---

## See also

- [setup.md](setup.md) — credential and data-access setup (shared with `uv` path)
- [gdal-install.md](gdal-install.md) — manual GDAL build (only needed if **not** using pixi)
- [cli.md](cli.md) — full CLI reference with all flags and sensor options
- [development.md](development.md) — `uv` setup, devcontainers, testing and CI
- [../README.md](../README.md) — project overview and quick start
