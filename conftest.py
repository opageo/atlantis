"""Conftest file for pytest configuration, workaround for having e2e tests collected but not run by default."""

import pytest


def pytest_addoption(parser):
    """Add a command-line option to control whether e2e tests are run."""
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="run end-to-end tests",
    )


def pytest_collection_modifyitems(config, items):
    """Skip e2e tests unless --run-e2e is specified."""
    if config.getoption("--run-e2e"):
        return

    skip_e2e = pytest.mark.skip(reason="need --run-e2e option to run")

    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)
