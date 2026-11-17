"""What the skill's reference tells a reader of the twin holds every endpoint the API
serves and every error code it answers with."""

from __future__ import annotations

import re
from pathlib import Path

from dta_runtime.api.problems import STATUS_BY_CODE
from starlette.testclient import TestClient

REFERENCE = (
    Path(__file__).resolve().parents[3] / "toolkit/src/dta_toolkit/templates/skill/reference"
)
_ROW = re.compile(r"^\| `(GET|POST|DELETE) (/[^`?\s]*)", re.MULTILINE)
_PARAMETER = re.compile(r"\{[^}/]*\}")
# Answered by the API and of no use to a reader of the twin: they say that the API's
# own configuration is wrong, and are told to whoever starts it.
_NOT_FOR_READERS = ("config.",)
# Codes of the reading routes that the reference has never listed: one that is no longer
# given, and one of a twin that holds no manifest, which no installed twin is.
_NEVER_LISTED = {"api.sort_too_large", "twin.not_registered"}


def test_every_endpoint_has_a_line_in_the_reference(
    app_client: TestClient, auth: dict[str, str]
) -> None:
    document = app_client.get("/api/v1/openapi.json", headers=auth).json()
    served = {
        (method.upper(), _PARAMETER.sub("{}", path.removeprefix("/api/v1")))
        for path, operations in document["paths"].items()
        for method in operations
        if path != "/healthz"
    }
    text = (REFERENCE / "api.md.j2").read_text(encoding="utf-8")
    listed = {(method, _PARAMETER.sub("{}", path)) for method, path in _ROW.findall(text)}

    assert served - listed == set()


def test_every_error_code_has_a_line_in_the_reference() -> None:
    text = (REFERENCE / "errors.md.j2").read_text(encoding="utf-8")
    told = set(re.findall(r"`([a-z_]+\.[a-z_]+)`", text))
    answered = {code for code in STATUS_BY_CODE if not code.startswith(_NOT_FOR_READERS)}

    assert sorted(answered - told - _NEVER_LISTED) == []
