"""Whether every endpoint the generated skill lists is one the running API serves.

The skill's `reference/api.md` lists endpoints in the first column of its
tables, each cell starting with a code span such as `GET /objects/{type}?…`.
Those are compared with the API's own OpenAPI document. The skill writes paths
under the API root and names path parameters its own way, so a skill path is
put under the root and every `{…}` segment on either side counts as the same.
"""

from __future__ import annotations

import re
from typing import Any

from dta_core.log import Log
from markdown_it import MarkdownIt

from .client import TwinApi
from .readiness import API
from .result import CheckResult, problem_of

log = Log("acceptance.endpoints")

SKILL_API_FILE = "reference/api.md"
# Served beside the API, not described by its document.
OUTSIDE_DOCUMENT = frozenset({("GET", "/healthz")})
_ENDPOINT = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (/\S*)")
_PARAMETER = re.compile(r"\{[^}/]*\}")
_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
REMEDY = (
    "the skill and the API disagree: generate the skill again with the toolkit version that "
    "built the twin, or report the difference"
)


def listed_endpoints(text: str) -> list[tuple[str, str]]:
    """(method, path) of every table row whose first cell starts with an endpoint, in order."""
    tokens = MarkdownIt("commonmark").enable("table").parse(text)
    found: dict[tuple[str, str], None] = {}
    first_cell = False
    for index, token in enumerate(tokens):
        if token.type == "tr_open":
            first_cell = True
        elif token.type in ("th_open", "td_open") and first_cell:
            first_cell = False
            children = tokens[index + 1].children or []
            if not children or children[0].type != "code_inline":
                continue
            match = _ENDPOINT.match(children[0].content)
            if match is None:
                continue
            method, rest = match.groups()
            found.setdefault((method, re.split(r"[?\s]", rest, maxsplit=1)[0]))
    return list(found)


def _shape(path: str) -> str:
    return _PARAMETER.sub("{}", path.rstrip("/") or "/")


def served_endpoints(document: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (method.upper(), _shape(path))
        for path, operations in (document.get("paths") or {}).items()
        for method in (operations or {})
        if method.lower() in _METHODS
    }


def _under_root(path: str) -> str:
    return path if path == API or path.startswith(f"{API}/") else f"{API}{path}"


def skill_endpoints(api: TwinApi, text: str | None, where: str) -> CheckResult:
    examined = (
        f"every endpoint the generated skill lists in {SKILL_API_FILE} is served by the API "
        f"(GET {API}/openapi.json)"
    )
    if text is None:
        return CheckResult(
            "skill.endpoints",
            examined,
            True,
            {"applicable": False, "reason": "no skill has been generated"},
        )
    listed = listed_endpoints(text)
    evidence: dict[str, Any] = {"file": where, "listed": len(listed)}
    response = api.get(f"{API}/openapi.json")
    if response.status_code != 200:
        evidence.update(
            {"failing_request": f"GET {API}/openapi.json", "response": problem_of(response)}
        )
        log.warn("skill endpoints check failed: no OpenAPI document", evidence=evidence)
        return CheckResult("skill.endpoints", examined, False, evidence, REMEDY)
    served = served_endpoints(response.json())
    missing = [
        f"{method} {path}"
        for method, path in listed
        if (method, path) not in OUTSIDE_DOCUMENT
        and (method, _shape(_under_root(path))) not in served
    ]
    evidence.update({"served": len(served), "missing": missing})
    passed = bool(listed) and not missing
    if not listed:
        evidence["reason"] = f"{SKILL_API_FILE} lists no endpoint in its tables"
    elif missing:
        evidence["reason"] = f"not served: {', '.join(missing)}"
    if not passed:
        log.warn("skill endpoints check failed", evidence=evidence)
    return CheckResult("skill.endpoints", examined, passed, evidence, REMEDY)
