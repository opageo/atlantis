.PHONY: help test lint lint-fix format-fix precommit build clean setup \
	demo example-harvey example-bihar example-vamco example-westafrica examples \
	example-harvey-bbox example-bihar-bbox example-vamco-bbox \
	example-westafrica-bbox examples-bbox

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
