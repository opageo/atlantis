# Project Atlantis

ML-ready archive of satellite-derived flood inundation
observations (ECMWF Code for Earth 2026).

## Installation

```bash
uv sync
```

## CLI

- `atlantis fetch` — fetch raw inundation data (placeholder)
- `atlantis archive` — harmonise and write ML-ready archive (placeholder)
- `atlantis validate` — validate the archive (placeholder)

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
