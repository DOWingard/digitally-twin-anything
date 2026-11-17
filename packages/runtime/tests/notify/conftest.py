"""Fixtures of the notification tests: the process's log lines in a file of the test's own."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from dta_core import log as core_log

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture
def log_file(tmp_path: Path) -> Iterator[Path]:
    """Every line the process logs during the test, as JSON, in the returned file."""
    root = logging.getLogger("dta")
    saved = (list(root.handlers), root.propagate, root.level)
    path = tmp_path / "notify.jsonl"
    core_log.configure(log_file=path, level="debug")
    try:
        yield path
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        handlers, propagate, level = saved
        for handler in handlers:
            root.addHandler(handler)
        root.propagate = propagate
        root.setLevel(level)
