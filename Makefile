.PHONY: help test lint lint-fix format-fix precommit build clean setup demo

help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

setup:  ## Bootstrap data assets and install dependencies
	uv sync --extra geo
	uv run python scripts/setup.py

demo:  ## Run the Valencia 2024 flood demo
	uv run atlantis demo

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
