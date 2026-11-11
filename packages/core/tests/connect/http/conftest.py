"""Fixtures of the HTTP connector tests: real JSON interfaces on free loopback ports.

The log capture (`captured_logs`) comes from the connector tests' conftest one
directory up.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from dta_http_testkit import Api  # noqa: E402


@pytest.fixture
def serve() -> Iterator[Callable[[], Api]]:
    """Start as many interfaces as a test needs; all are stopped afterwards."""
    started: list[Api] = []

    def start() -> Api:
        api = Api()
        started.append(api)
        return api

    yield start
    for api in started:
        api.close()


@pytest.fixture
def api(serve: Callable[[], Api]) -> Api:
    return serve()
