"""The acceptance tests share the small test twin of the command tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from dta_core.log import configure

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "commands"))


@pytest.fixture(autouse=True)
def _logs_to_current_stderr() -> None:
    # A command run through CliRunner leaves the log handler on a stream it has closed.
    configure()
