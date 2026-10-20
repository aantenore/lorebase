"""Performance tests run only when selected: `pytest tests/perf -m perf`."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if "perf" in (config.getoption("markexpr") or ""):
        return
    skip = pytest.mark.skip(reason="performance test; select with -m perf")
    for item in items:
        if item.get_closest_marker("perf") is not None:
            item.add_marker(skip)
