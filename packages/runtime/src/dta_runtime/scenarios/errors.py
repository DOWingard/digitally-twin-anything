"""Why a scenario was refused or its run failed, as stable codes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from dta_core.errors import DtaError

BAD_REQUEST = "scenario.bad_request"
NOT_INTERVENTIONAL = "scenario.not_interventional"
NOT_FOUND = "scenario.not_found"
RUNNING = "scenario.running"
ALREADY_RUN = "scenario.already_run"
DELETED = "scenario.deleted"
TIMEOUT = "scenario.timeout"
BAD_PREDICTION = "scenario.bad_prediction"


class ScenarioError(DtaError):
    """A scenario was refused or failed; `detail` names the scenario when there is one.

    A `scenario.bad_request` carries `detail["problems"]`: every problem found, each
    with the `member` of the request it is about and a `message`.
    """

    code = "scenario.error"


def refused(code: str, message: str, **detail: Any) -> ScenarioError:
    return ScenarioError(message, code=code, detail=detail)


def problem(member: str, message: str) -> dict[str, str]:
    return {"member": member, "message": message}


def bad_request(problems: Sequence[dict[str, str]], **detail: Any) -> ScenarioError:
    """One refusal for every problem found."""
    first = problems[0]
    more = "" if len(problems) == 1 else f" (and {len(problems) - 1} more)"
    return ScenarioError(
        f"{first['member']}: {first['message']}{more}",
        code=BAD_REQUEST,
        detail={"problems": list(problems), **detail},
    )


def bad_prediction(message: str, *, model: str, **detail: Any) -> ScenarioError:
    return ScenarioError(
        f"{model}: {message}", code=BAD_PREDICTION, detail={"model": model, **detail}
    )
