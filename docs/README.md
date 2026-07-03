# Atlantis Documentation

Reference guide for Atlantis data sources, processing pipelines, and shared
design notes.

The layer registry is now the main source of truth for what Atlantis can emit
per source. Start with [Layers](./layers.md) or `atlantis list-layers` if you
need to confirm the native layers fetched from a source versus the derived
layers Atlantis computes from them.

## Data Sources

- [VIIRS](./viirs/overview.md) - Optical flood mapping guides, API usage,
  internals, and pipeline behavior.
- [MODIS](./modis/overview.md) - MODIS MCDWD product guide, API usage,
  internals, and pipeline details.
- [GFM](./gfm/overview.md) - SAR-based flood mapping guide, API usage,
  internals, and processing pipeline.

## Architecture and Design

- [Layers](./layers.md) - Per-source catalogue of native and derived layers
  (auto-generated from the layer registries; regenerate with
  `python scripts/generate_layer_docs.py` or view via `atlantis list-layers`).
- [KuroSiwo STAC design](./kurosiwo-stac-design.md)
- [STAC architecture diagrams](./mermaid_stac_ks_labeled.md)
- [Compact STAC mermaid diagram](./mermaid_stac_ks.md)

## Getting Started

- [pixi-setup.md](./pixi-setup.md) - New user onboarding path with a bundled
  GDAL + HDF4 environment (no manual GDAL build).
- [setup.md](./setup.md) - Shared credentials and one-time account setup
  (Earthdata token, LAADS Web pre-authorization, AWS profiles, KuroSiwo
  catalogue) used by both `uv` and `pixi` workflows.
- [gdal-install.md](./gdal-install.md) - Manual GDAL build guide for advanced
  `uv` setups that need explicit HDF4 support.
- [src/README.md](../src/README.md) - Architecture guide, module layout, CLI
  examples, and extension points.
- [cli.md](./cli.md) - Full CLI reference: every command, every flag,
  defaults, and sensor-specific options.
- [CLI_Examples.md](../CLI_Examples.md) - End-to-end example commands across
  real flood events.
- [development.md](./development.md) - Contributor guide: running tests,
  E2E workflow, and testing GitHub Actions locally.
