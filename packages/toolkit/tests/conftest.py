"""What every toolkit test shares."""

from __future__ import annotations

from typing import Any

import pytest
from dta_core.manifest.models import MANIFEST_VERSION
from dta_toolkit import runtimecheck


@pytest.fixture(autouse=True)
def _image_reads_this_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """The images the suites' stacks run are built apart from the code under test, so what
    an image reads is answered here; the tests of that check ask an image themselves."""

    def answer(_stack: Any, _image: str) -> dict[str, Any]:
        return {"package": "dta-runtime", "version": "test", "manifest_version": MANIFEST_VERSION}

    monkeypatch.setattr(runtimecheck, "answer", answer)
