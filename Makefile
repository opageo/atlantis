.PHONY: help test lint lint-fix format-fix precommit build clean setup \
	demo example-harvey example-bihar example-vamco example-westafrica examples \
	example-harvey-bbox example-bihar-bbox example-vamco-bbox \
	example-westafrica-bbox examples-bbox \
	example-harvey-modis example-bihar-modis example-vamco-modis \
	example-westafrica-modis examples-modis \
	example-harvey-modis-bbox example-bihar-modis-bbox example-vamco-modis-bbox \
	example-westafrica-modis-bbox examples-modis-bbox \
	example-modis-recent example-modis-recent-post example-modis-recent-balanced \
	examples-modis-recent \
	example-gfm-valencia example-gfm-valencia-peak

help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

setup:  ## Bootstrap data assets and install dependencies
	uv sync --extra geo
	uv run python scripts/setup.py

demo:  ## Run the Valencia 2024 flood demo (see CLI_Examples.md for more case studies)
	uv run atlantis --verbose demo

example-harvey:  ## Example: Hurricane Harvey, Texas USA — Aug 2017 (KuroSiwo_1111004)
	uv run atlantis --verbose fetch-kurosiwo-viirs \
		--case KuroSiwo_1111004 \
		--days-before 1 --days-after 1 \
		--plot --harmonise --no-keep-processed \
		--output ./data/KuroSiwo_1111004

example-bihar:  ## Example: South Asian monsoon, Bihar/Nepal — Sept 2019 (KuroSiwo_1111007)
	uv run atlantis --verbose fetch-kurosiwo-viirs \
		--case KuroSiwo_1111007 \
		--days-before 2 --days-after 2 \
		--plot --harmonise --no-keep-processed \
		--output ./data/KuroSiwo_1111007

example-vamco:  ## Example: Typhoon Vamco, Luzon Philippines — Nov 2020 (KuroSiwo_1111011)
	uv run atlantis --verbose fetch-kurosiwo-viirs \
		--case KuroSiwo_1111011 \
		--days-before 1 --days-after 1 \
		--plot --harmonise --no-keep-processed \
		--output ./data/KuroSiwo_1111011

example-westafrica:  ## Example: West Africa floods, Ghana/Togo/Benin — Oct 2020 (KuroSiwo_470)
	uv run atlantis --verbose fetch-kurosiwo-viirs \
		--case KuroSiwo_470 \
		--plot --harmonise --no-keep-processed \
		--output ./data/KuroSiwo_470

examples: demo example-harvey example-bihar example-vamco example-westafrica  ## Run all CLI_Examples.md case studies

# ---- Generic CLI examples (bbox + date, no KuroSiwo catalogue) ----
# These showcase the default `atlantis fetch` command — same flood events as
# the targets above, but the user only supplies a bounding box and date range.

example-harvey-bbox:  ## Generic CLI: Hurricane Harvey, Texas USA — Aug 2017 (bbox + dates)
	uv run atlantis --verbose fetch \
		--event Harvey_2017 \
		--source viirs \
		--bbox "-97.27 28.24 -95.54 29.80" \
		--start-date 2017-08-28 --end-date 2017-08-31 \
		--plot --harmonise --no-keep-processed \
		--output ./data/Harvey_2017

example-bihar-bbox:  ## Generic CLI: South Asian monsoon, Bihar/Nepal — Sept 2019 (bbox + dates)
	uv run atlantis --verbose fetch \
		--event Bihar_2019 \
		--source viirs \
		--bbox "84.84 24.92 86.49 26.16" \
		--start-date 2019-09-16 --end-date 2019-09-20 \
		--strategy aggregate \
		--plot --harmonise --no-keep-processed \
		--output ./data/Bihar_2019

example-vamco-bbox:  ## Generic CLI: Typhoon Vamco, Luzon Philippines — Nov 2020 (bbox + dates)
	uv run atlantis --verbose fetch \
		--event Vamco_2020 \
		--source viirs \
		--bbox "121.14 16.72 122.25 18.45" \
		--start-date 2020-11-12 --end-date 2020-11-14 \
		--plot --harmonise --no-keep-processed \
		--output ./data/Vamco_2020

example-westafrica-bbox:  ## Generic CLI: West Africa floods, Ghana/Togo/Benin — Oct 2020 (bbox + dates)
	uv run atlantis --verbose fetch \
		--event WestAfrica_2020 \
		--source viirs \
		--bbox "-0.86 8.26 1.99 11.73" \
		--start-date 2020-10-13 --end-date 2020-10-15 \
		--plot --harmonise --no-keep-processed \
		--output ./data/WestAfrica_2020

examples-bbox: demo example-harvey-bbox example-bihar-bbox example-vamco-bbox example-westafrica-bbox  ## Run all generic-CLI (bbox+date) case studies

# ---- MODIS LANCE NRT showcase (works after `make setup`, no HDF4 required) ----
# These targets exercise the live LANCE NRT feed (last ~1 week of MODIS data)
# and showcase the --verbose flag plus the peak-window / max-observations
# selection options. Dates are computed at make-time relative to today so the
# requests always land inside the rolling NRT window.

MODIS_LANCE_END_DATE   := $(shell date -u +%Y-%m-%d)
MODIS_LANCE_START_DATE := $(shell date -u -d "5 days ago" +%Y-%m-%d)
MODIS_LANCE_BBOX       := -97.27 28.24 -95.54 29.80
MODIS_LANCE_OUTPUT     := ./data/MODIS_recent

example-modis-recent:  ## MODIS LANCE NRT: --verbose + symmetric peak window (±1 day) around peak
	uv run atlantis --verbose fetch \
		--event MODIS_recent \
		--source modis \
		--bbox "$(MODIS_LANCE_BBOX)" \
		--start-date $(MODIS_LANCE_START_DATE) --end-date $(MODIS_LANCE_END_DATE) \
		--modis-backend lance_geotiff --modis-composite F2 \
		--strategy all --peak-window-days 1 \
		--plot --harmonise --no-keep-processed \
		--output $(MODIS_LANCE_OUTPUT)

example-modis-recent-post:  ## MODIS LANCE NRT: --verbose + max 2 post-event observations
	uv run atlantis --verbose fetch \
		--event MODIS_recent_post \
		--source modis \
		--bbox "$(MODIS_LANCE_BBOX)" \
		--start-date $(MODIS_LANCE_START_DATE) --end-date $(MODIS_LANCE_END_DATE) \
		--modis-backend lance_geotiff --modis-composite F2 \
		--strategy all \
		--peak-days-before 0 --peak-days-after 3 \
		--max-observations 2 --peak-priority post \
		--plot --harmonise --no-keep-processed \
		--output $(MODIS_LANCE_OUTPUT)_post

example-modis-recent-balanced:  ## MODIS LANCE NRT: --verbose + max 3 obs (balanced ±N around peak)
	uv run atlantis --verbose fetch \
		--event MODIS_recent_balanced \
		--source modis \
		--bbox "$(MODIS_LANCE_BBOX)" \
		--start-date $(MODIS_LANCE_START_DATE) --end-date $(MODIS_LANCE_END_DATE) \
		--modis-backend lance_geotiff --modis-composite F2 \
		--strategy all --peak-window-days 2 \
		--max-observations 3 --peak-priority balanced \
		--plot --harmonise --no-keep-processed \
		--output $(MODIS_LANCE_OUTPUT)_balanced

examples-modis-recent: example-modis-recent example-modis-recent-post example-modis-recent-balanced  ## Run all MODIS LANCE NRT showcase examples

# ---- GFM showcase (Sentinel-1 SAR via EODC STAC, anonymous public API) ----
# These targets exercise the GFM fetcher with --verbose and the peak-window
# / max-observations selection options. No credentials required.

GFM_BBOX        := -1.5 38.8 0.5 40.0
GFM_START_DATE  := 2024-10-29
GFM_END_DATE    := 2024-11-04
GFM_OUTPUT      := ./data/Valencia_2024_gfm

example-gfm-valencia:  ## GFM: Valencia floods, Oct–Nov 2024 (aggregate strategy, --verbose)
	uv run atlantis --verbose fetch \
		--event Valencia_2024 \
		--source gfm \
		--bbox "$(GFM_BBOX)" \
		--start-date $(GFM_START_DATE) --end-date $(GFM_END_DATE) \
		--strategy aggregate \
		--plot --harmonise --no-keep-processed \
		--output $(GFM_OUTPUT)

example-gfm-valencia-peak:  ## GFM: Valencia floods + peak window (±1 day) + max 3 obs (balanced)
	uv run atlantis --verbose fetch \
		--event Valencia_2024 \
		--source gfm \
		--bbox "$(GFM_BBOX)" \
		--start-date $(GFM_START_DATE) --end-date $(GFM_END_DATE) \
		--strategy all --peak-window-days 1 \
		--max-observations 3 --peak-priority balanced \
		--plot --harmonise --no-keep-processed \
		--output $(GFM_OUTPUT)_peak

# ---- MODIS KuroSiwo examples ----
# Same case studies as the VIIRS targets above, but using the MODIS MCDWD
# fetcher. Historical events (pre-2026) require --modis-backend laads_hdf4
# because the LANCE NRT window only covers ~1 week of recent data. Requires
# EARTHDATA_TOKEN (run `make setup` first).

example-harvey-modis:  ## MODIS: Hurricane Harvey, Texas USA — Aug 2017 (KuroSiwo_1111004)
	uv run atlantis --verbose fetch-kurosiwo-modis \
		--case KuroSiwo_1111004 \
		--days-before 1 --days-after 1 \
		--modis-backend laads_hdf4 --modis-composite F2 \
		--plot --harmonise --no-keep-processed \
		--output ./data/KuroSiwo_1111004

example-bihar-modis:  ## MODIS: South Asian monsoon, Bihar/Nepal — Sept 2019 (KuroSiwo_1111007)
	uv run atlantis --verbose fetch-kurosiwo-modis \
		--case KuroSiwo_1111007 \
		--days-before 2 --days-after 2 \
		--modis-backend laads_hdf4 --modis-composite F2 \
		--plot --harmonise --no-keep-processed \
		--output ./data/KuroSiwo_1111007

example-vamco-modis:  ## MODIS: Typhoon Vamco, Luzon Philippines — Nov 2020 (KuroSiwo_1111011)
	uv run atlantis --verbose fetch-kurosiwo-modis \
		--case KuroSiwo_1111011 \
		--days-before 1 --days-after 1 \
		--modis-backend laads_hdf4 --modis-composite F2 \
		--plot --harmonise --no-keep-processed \
		--output ./data/KuroSiwo_1111011

example-westafrica-modis:  ## MODIS: West Africa floods, Ghana/Togo/Benin — Oct 2020 (KuroSiwo_470)
	uv run atlantis --verbose fetch-kurosiwo-modis \
		--case KuroSiwo_470 \
		--modis-backend laads_hdf4 --modis-composite F2 \
		--plot --harmonise --no-keep-processed \
		--output ./data/KuroSiwo_470

examples-modis: example-harvey-modis example-bihar-modis example-vamco-modis example-westafrica-modis  ## Run all MODIS KuroSiwo examples

# ---- MODIS generic CLI examples (bbox + date) ----

example-harvey-modis-bbox:  ## MODIS bbox: Hurricane Harvey, Texas USA — Aug 2017
	uv run atlantis --verbose fetch \
		--event Harvey_2017 \
		--source modis \
		--bbox "-97.27 28.24 -95.54 29.80" \
		--start-date 2017-08-28 --end-date 2017-08-31 \
		--modis-backend laads_hdf4 --modis-composite F2 \
		--plot --harmonise --no-keep-processed \
		--output ./data/Harvey_2017

example-bihar-modis-bbox:  ## MODIS bbox: South Asian monsoon, Bihar/Nepal — Sept 2019
	uv run atlantis --verbose fetch \
		--event Bihar_2019 \
		--source modis \
		--bbox "84.84 24.92 86.49 26.16" \
		--start-date 2019-09-16 --end-date 2019-09-20 \
		--modis-backend laads_hdf4 --modis-composite F2 \
		--strategy aggregate \
		--plot --harmonise --no-keep-processed \
		--output ./data/Bihar_2019

example-vamco-modis-bbox:  ## MODIS bbox: Typhoon Vamco, Luzon Philippines — Nov 2020
	uv run atlantis --verbose fetch \
		--event Vamco_2020 \
		--source modis \
		--bbox "121.14 16.72 122.25 18.45" \
		--start-date 2020-11-12 --end-date 2020-11-14 \
		--modis-backend laads_hdf4 --modis-composite F2 \
		--plot --harmonise --no-keep-processed \
		--output ./data/Vamco_2020

example-westafrica-modis-bbox:  ## MODIS bbox: West Africa floods, Ghana/Togo/Benin — Oct 2020
	uv run atlantis --verbose fetch \
		--event WestAfrica_2020 \
		--source modis \
		--bbox "-0.86 8.26 1.99 11.73" \
		--start-date 2020-10-13 --end-date 2020-10-15 \
		--modis-backend laads_hdf4 --modis-composite F2 \
		--plot --harmonise --no-keep-processed \
		--output ./data/WestAfrica_2020

examples-modis-bbox: example-harvey-modis-bbox example-bihar-modis-bbox example-vamco-modis-bbox example-westafrica-modis-bbox  ## Run all generic-CLI (bbox+date) MODIS case studies

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
