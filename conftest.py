"""Conftest file for pytest configuration, workaround for having e2e tests collected but not run by default."""

import pytest


def pytest_collection_modifyitems(config, items):
    """Skip e2e tests by default, without affecting test collection/discovery.

    All tests (including e2e) are still collected, so tools like the VS Code
    Python test explorer/debugger can discover and run them individually.
    When no explicit `-m` marker expression is given, e2e tests are skipped.
    Passing an expression that references "e2e" (e.g. `-m e2e` or
    `-m "not e2e"`) opts back into pytest's own marker filtering, so e.g.
    `pytest -m e2e` runs only the e2e tests.
    """
    markexpr = config.getoption("markexpr") or ""
    if "e2e" in markexpr:
        return

    skip_e2e = pytest.mark.skip(reason="e2e tests skipped by default; run with `-m e2e` to include them")

    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)
