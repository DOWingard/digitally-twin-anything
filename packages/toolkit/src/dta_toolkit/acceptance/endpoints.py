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
# Route groups a live twin's skill must cover whole, and the lifecycle routes every
# twin's skill must list.
LIVE_GROUPS = ("fidelity", "actions", "control")
LIFECYCLE_REQUIRED = ("schedule", "notifications", "register")


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


def required_endpoints(served: set[tuple[str, str]], live: bool) -> list[tuple[str, str]]:
    """The served endpoints the skill must list: of a live twin, every one of fidelity,
    actions and control loops; of any twin, the schedule, notifications and registration.

    A static twin's API serves the fidelity, action and loop routes too, but on it no
    check and no loop ever runs and nothing is executed, so its skill does not offer
    them and they are not required of it.
    """
    lifecycle = {f"{API}/lifecycle/{name}" for name in LIFECYCLE_REQUIRED}
    groups = tuple(f"{API}/{group}" for group in LIVE_GROUPS)
    found = []
    for method, path in sorted(served):
        if path in lifecycle or (
            live and any(path == group or path.startswith(f"{group}/") for group in groups)
        ):
            found.append((method, path))
    return found


def skill_endpoints(
    api: TwinApi, text: str | None, where: str, *, live: bool = False
) -> CheckResult:
    examined = (
        f"every endpoint the generated skill lists in {SKILL_API_FILE} is served by the API "
        f"(GET {API}/openapi.json), and it lists every route of the schedule, notifications and "
        f"registration{', fidelity, actions and control loops' if live else ''}"
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
    shown = {(method, _shape(_under_root(path))) for method, path in listed}
    required = required_endpoints(served, live)
    unlisted = [f"{method} {path}" for method, path in required if (method, path) not in shown]
    evidence.update(
        {
            "served": len(served),
            "missing": missing,
            "required": len(required),
            "unlisted": unlisted,
            "tier": "live" if live else "static",
        }
    )
    passed = bool(listed) and not missing and not unlisted
    reasons = []
    if not listed:
        reasons.append(f"{SKILL_API_FILE} lists no endpoint in its tables")
    if missing:
        reasons.append(f"not served: {', '.join(missing)}")
    if unlisted:
        reasons.append(f"served and not listed: {', '.join(unlisted)}")
    if reasons:
        evidence["reason"] = "; ".join(reasons)
    if not passed:
        log.warn("skill endpoints check failed", evidence=evidence)
    return CheckResult("skill.endpoints", examined, passed, evidence, REMEDY)
