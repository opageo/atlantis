# ============================================================================
# Atlantis — Makefile
#
# Layout:
#   * Toolchain targets:  setup, test, lint, build, ...
#   * Demo:               quick smoke test (Valencia 2024 flood)
#   * Examples:           one target per (event, source) pair, named
#                         `example-<event>-<source>`. All examples share the
#                         same shape (verbose + peak-window selection +
#                         harmonisation + plots) so VIIRS, MODIS and GFM
#                         behave identically from the user's point of view.
#
# Note on MODIS: there is no S3+GeoTIFF backend for the historical archive.
# Pre-NRT events (< ~1 week old) must use `--modis-backend laads_hdf4`,
# which requires `EARTHDATA_TOKEN` (run `make setup` once). The `lance_geotiff`
# backend is GeoTIFF-native but only covers the rolling LANCE NRT window.
# ============================================================================

.PHONY: help setup demo test lint lint-fix format-fix precommit build clean \
	dev-install version \
	example-harvey-viirs example-bihar-viirs example-vamco-viirs \
	example-westafrica-viirs examples-viirs \
	example-harvey-modis example-bihar-modis example-vamco-modis \
	example-westafrica-modis example-modis-recent examples-modis \
	example-harvey-gfm example-bihar-gfm example-vamco-gfm \
	example-westafrica-gfm example-valencia-gfm examples-gfm \
	examples

# ---- Shared CLI flags ------------------------------------------------------
# Peak-window selection: ±2 days around the modelled peak, capped at 3
# observations split symmetrically (balanced) before/after the peak.
PEAK_FLAGS    := --strategy all --peak-window-days 2 --max-observations 3 --peak-priority balanced
COMMON_FLAGS  := --plot --harmonise --no-keep-processed

# MODIS backend selection (see header note).
MODIS_HIST    := --modis-backend laads_hdf4 --modis-composite F2
MODIS_NRT     := --modis-backend lance_geotiff --modis-composite F2

# ---- Event bboxes and date ranges -----------------------------------------
HARVEY_BBOX        := -97.27 28.24 -95.54 29.80
HARVEY_START       := 2017-08-28
HARVEY_END         := 2017-08-31

BIHAR_BBOX         := 84.84 24.92 86.49 26.16
BIHAR_START        := 2019-09-16
BIHAR_END          := 2019-09-20

VAMCO_BBOX         := 121.14 16.72 122.25 18.45
VAMCO_START        := 2020-11-12
VAMCO_END          := 2020-11-14

WESTAFRICA_BBOX    := -0.86 8.26 1.99 11.73
WESTAFRICA_START   := 2020-10-13
WESTAFRICA_END     := 2020-10-15

VALENCIA_BBOX      := -1.5 38.8 0.5 40.0
VALENCIA_START     := 2024-10-29
VALENCIA_END       := 2024-11-04

# Rolling LANCE NRT window — recomputed each invocation so it always lands
# inside the ~1 week of available NRT data. Reuses Harvey's bbox.
NRT_END            := $(shell date -u +%Y-%m-%d)
NRT_START          := $(shell date -u -d "5 days ago" +%Y-%m-%d)
NRT_BBOX           := $(HARVEY_BBOX)

# ============================================================================
# Toolchain
# ============================================================================

help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-28s\033[0m %s\n", $$1, $$2}'

setup:  ## Bootstrap data assets and install dependencies
	uv sync --extra geo
	uv run python scripts/setup.py

demo:  ## Run the Valencia 2024 flood demo (see CLI_Examples.md for more case studies)
	uv run atlantis --verbose demo

# ============================================================================
# VIIRS examples — historical bbox + date range, no credentials required
# ============================================================================

example-harvey-viirs:  ## VIIRS: Hurricane Harvey, Texas USA — Aug 2017
	uv run atlantis --verbose fetch \
		--event Harvey_2017 --source viirs \
		--bbox "$(HARVEY_BBOX)" \
		--start-date $(HARVEY_START) --end-date $(HARVEY_END) \
		$(PEAK_FLAGS) $(COMMON_FLAGS) \
		--output ./data/Harvey_2017

example-bihar-viirs:  ## VIIRS: South Asian monsoon, Bihar/Nepal — Sept 2019
	uv run atlantis --verbose fetch \
		--event Bihar_2019 --source viirs \
		--bbox "$(BIHAR_BBOX)" \
		--start-date $(BIHAR_START) --end-date $(BIHAR_END) \
		$(PEAK_FLAGS) $(COMMON_FLAGS) \
		--output ./data/Bihar_2019

example-vamco-viirs:  ## VIIRS: Typhoon Vamco, Luzon Philippines — Nov 2020
	uv run atlantis --verbose fetch \
		--event Vamco_2020 --source viirs \
		--bbox "$(VAMCO_BBOX)" \
		--start-date $(VAMCO_START) --end-date $(VAMCO_END) \
		$(PEAK_FLAGS) $(COMMON_FLAGS) \
		--output ./data/Vamco_2020

example-westafrica-viirs:  ## VIIRS: West Africa floods, Ghana/Togo/Benin — Oct 2020
	uv run atlantis --verbose fetch \
		--event WestAfrica_2020 --source viirs \
		--bbox "$(WESTAFRICA_BBOX)" \
		--start-date $(WESTAFRICA_START) --end-date $(WESTAFRICA_END) \
		$(PEAK_FLAGS) $(COMMON_FLAGS) \
		--output ./data/WestAfrica_2020

examples-viirs: example-harvey-viirs example-bihar-viirs example-vamco-viirs example-westafrica-viirs  ## Run all VIIRS examples

# ============================================================================
# MODIS examples — historical events use LAADS HDF4 (needs EARTHDATA_TOKEN);
# `example-modis-recent` exercises the LANCE NRT GeoTIFF backend.
# ============================================================================

example-harvey-modis:  ## MODIS: Hurricane Harvey, Texas USA — Aug 2017 (LAADS HDF4)
	uv run atlantis --verbose fetch \
		--event Harvey_2017 --source modis \
		--bbox "$(HARVEY_BBOX)" \
		--start-date $(HARVEY_START) --end-date $(HARVEY_END) \
		$(MODIS_HIST) $(PEAK_FLAGS) $(COMMON_FLAGS) \
		--output ./data/Harvey_2017

example-bihar-modis:  ## MODIS: South Asian monsoon, Bihar/Nepal — Sept 2019 (LAADS HDF4)
	uv run atlantis --verbose fetch \
		--event Bihar_2019 --source modis \
		--bbox "$(BIHAR_BBOX)" \
		--start-date $(BIHAR_START) --end-date $(BIHAR_END) \
		$(MODIS_HIST) $(PEAK_FLAGS) $(COMMON_FLAGS) \
		--output ./data/Bihar_2019

example-vamco-modis:  ## MODIS: Typhoon Vamco, Luzon Philippines — Nov 2020 (LAADS HDF4)
	uv run atlantis --verbose fetch \
		--event Vamco_2020 --source modis \
		--bbox "$(VAMCO_BBOX)" \
		--start-date $(VAMCO_START) --end-date $(VAMCO_END) \
		$(MODIS_HIST) $(PEAK_FLAGS) $(COMMON_FLAGS) \
		--output ./data/Vamco_2020

example-westafrica-modis:  ## MODIS: West Africa floods, Ghana/Togo/Benin — Oct 2020 (LAADS HDF4)
	uv run atlantis --verbose fetch \
		--event WestAfrica_2020 --source modis \
		--bbox "$(WESTAFRICA_BBOX)" \
		--start-date $(WESTAFRICA_START) --end-date $(WESTAFRICA_END) \
		$(MODIS_HIST) $(PEAK_FLAGS) $(COMMON_FLAGS) \
		--output ./data/WestAfrica_2020

example-modis-recent:  ## MODIS: rolling LANCE NRT showcase (no credentials, last ~5 days)
	uv run atlantis --verbose fetch \
		--event MODIS_recent --source modis \
		--bbox "$(NRT_BBOX)" \
		--start-date $(NRT_START) --end-date $(NRT_END) \
		$(MODIS_NRT) $(PEAK_FLAGS) $(COMMON_FLAGS) \
		--output ./data/MODIS_recent

examples-modis: example-harvey-modis example-bihar-modis example-vamco-modis example-westafrica-modis example-modis-recent  ## Run all MODIS examples

# ============================================================================
# GFM examples — Sentinel-1 SAR via EODC STAC (anonymous public API)
# ============================================================================

example-harvey-gfm:  ## GFM: Hurricane Harvey, Texas USA — Aug 2017
	uv run atlantis --verbose fetch \
		--event Harvey_2017 --source gfm \
		--bbox "$(HARVEY_BBOX)" \
		--start-date $(HARVEY_START) --end-date $(HARVEY_END) \
		$(PEAK_FLAGS) $(COMMON_FLAGS) \
		--output ./data/Harvey_2017

example-bihar-gfm:  ## GFM: South Asian monsoon, Bihar/Nepal — Sept 2019
	uv run atlantis --verbose fetch \
		--event Bihar_2019 --source gfm \
		--bbox "$(BIHAR_BBOX)" \
		--start-date $(BIHAR_START) --end-date $(BIHAR_END) \
		$(PEAK_FLAGS) $(COMMON_FLAGS) \
		--output ./data/Bihar_2019

example-vamco-gfm:  ## GFM: Typhoon Vamco, Luzon Philippines — Nov 2020
	uv run atlantis --verbose fetch \
		--event Vamco_2020 --source gfm \
		--bbox "$(VAMCO_BBOX)" \
		--start-date $(VAMCO_START) --end-date $(VAMCO_END) \
		$(PEAK_FLAGS) $(COMMON_FLAGS) \
		--output ./data/Vamco_2020

example-westafrica-gfm:  ## GFM: West Africa floods, Ghana/Togo/Benin — Oct 2020
	uv run atlantis --verbose fetch \
		--event WestAfrica_2020 --source gfm \
		--bbox "$(WESTAFRICA_BBOX)" \
		--start-date $(WESTAFRICA_START) --end-date $(WESTAFRICA_END) \
		$(PEAK_FLAGS) $(COMMON_FLAGS) \
		--output ./data/WestAfrica_2020

example-valencia-gfm:  ## GFM: Valencia floods, Spain — Oct–Nov 2024
	uv run atlantis --verbose fetch \
		--event Valencia_2024 --source gfm \
		--bbox "$(VALENCIA_BBOX)" \
		--start-date $(VALENCIA_START) --end-date $(VALENCIA_END) \
		$(PEAK_FLAGS) $(COMMON_FLAGS) \
		--output ./data/Valencia_2024

examples-gfm: example-harvey-gfm example-bihar-gfm example-vamco-gfm example-westafrica-gfm example-valencia-gfm  ## Run all GFM examples

# ---- Aggregate -------------------------------------------------------------

examples: examples-viirs examples-modis examples-gfm  ## Run all examples across all sources

# ============================================================================
# Development
# ============================================================================

test:  ## Run tests (parallel)
	uv run poe test

lint:  ## Run linting
	uv run poe lint

lint-fix:  ## Run linting and auto-fix
	uv run poe lint-fix

format-fix:  ## Run formatter and auto-fix
	uv run poe format-fix

precommit:  ## Run pre-commit hooks on all files
	uv run poe precommit

build:  ## Build package
	uv build

clean:  ## Clean build artifacts
	rm -rf dist/ build/ *.egg-info/

dev-install:  ## Install with dev dependencies
	uv sync

version:  ## Show current version
	@python -c "import atlantis; print(f'Current version: {atlantis.__version__}')"

### docker
docker-build:  ## Build Docker image
	cd docker build -t atlantis:latest .