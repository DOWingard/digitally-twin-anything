"""Test settings shared by every package.

Tests marked `scale` load full-size fixtures and assert performance
thresholds. They are skipped unless selected with `-m scale`.
"""

from __future__ import annotations

import pytest

SCALE = "scale"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{SCALE}: loads a full-size fixture and asserts performance thresholds; "
        f"run with -m {SCALE}",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if SCALE in (config.getoption("-m") or ""):
        return
    skip = pytest.mark.skip(reason=f"full-size fixture; run with -m {SCALE}")
    for item in items:
        if SCALE in item.keywords:
            item.add_marker(skip)
