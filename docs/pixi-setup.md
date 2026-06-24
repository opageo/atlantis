# Setting up Atlantis with pixi

[Pixi](https://pixi.sh) provides a single-command setup that installs **all**
dependencies — including GDAL with HDF4 support — from conda-forge. No manual
compilation or system package management is needed.

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
- The `atlantis` package itself (editable install)

Verify the GDAL/HDF4 stack:

```bash
pixi run verify-gdal
# Expected: GDAL 3.x.x — HDF4 driver: OK
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

## 4. Run the demo

```bash
pixi run demo
```

Fetches VIIRS inundation data for the Valencia 2024 flood event, harmonises to
1 arcmin, and produces plots under `./data/Valencia_2024/`.

---

## 5. Development tasks

| Command                | Description                                |
| ---------------------- | ------------------------------------------ |
| `pixi run test`        | Run test suite (parallel via pytest-xdist) |
| `pixi run lint`        | Lint with ruff                             |
| `pixi run format`      | Auto-format with ruff                      |
| `pixi run precommit`   | Run all pre-commit hooks                   |
| `pixi run verify-gdal` | Confirm GDAL HDF4 driver is available      |

For per-event examples (Harvey, Bihar, Vamco, etc.) use the CLI directly:

```bash
pixi run atlantis --verbose fetch \
  --event Harvey_2017 --source viirs \
  --bbox "-97.27 28.24 -95.54 29.80" \
  --start-date 2017-08-28 --end-date 2017-08-31 \
  --strategy all --peak-window-days 2 --max-observations 3 --peak-priority balanced \
  --plot --harmonise --no-keep-processed \
  --output ./data/Harvey_2017
```

Or activate an interactive shell and use `make`:

```bash
pixi shell
make example-harvey-viirs
```

---

## 6. Optional environments

Beyond the default (geo + dev), three opt-in environments are available:

| Environment | Adds                                         | Activate with             |
| ----------- | -------------------------------------------- | ------------------------- |
| `ml`        | PyTorch (CPU), NumPy, scikit-learn           | `pixi shell -e ml`        |
| `notebooks` | earthkit-data, cartopy, metview¹             | `pixi shell -e notebooks` |
| `batch`     | Dask distributed, bokeh dashboard, rio-cogeo | `pixi shell -e batch`     |

¹ `metview-python` is only available on `linux-64`. On macOS (osx-arm64) the
notebooks environment installs everything except metview.

Run a task in a specific environment:

```bash
pixi run -e ml test
pixi run -e notebooks python notebooks/ecmwf/Extract_VIIRS_inundation.ipynb
```

---

## 7. Troubleshooting

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
- [../README.md](../README.md) — project overview and quick start
