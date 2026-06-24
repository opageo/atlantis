# Atlantis Documentation

Reference guide for Atlantis data sources, processing pipelines, and shared
design notes.

## Data Sources

- [VIIRS](./viirs/overview.md) - Optical flood mapping guides, API usage,
  internals, and pipeline behavior.
- [MODIS](./modis/overview.md) - MODIS MCDWD product guide, API usage,
  internals, and pipeline details.
- [GFM](./gfm/overview.md) - SAR-based flood mapping guide, API usage,
  internals, and processing pipeline.

## Architecture and Design

- [KuroSiwo STAC design](./kurosiwo/stac-design.md)
- [STAC architecture diagrams](./kurosiwo/stac_ks_labeled.md)
- [Compact STAC mermaid diagram](./kurosiwo/stac_ks.md)

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
- [CLI_Examples.md](../CLI_Examples.md) - End-to-end example commands across
  real flood events.
