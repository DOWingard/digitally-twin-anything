"""The scenarios of a twin: what-ifs kept apart from the live line.

A scenario is a branch of the versioned graph that starts at a version of the
live line (its base), holds assumed changes (its interventions) and is filled
with what models predict follows: one version per time of the twin's grid, from
the base time to the end of the horizon. Assumed facts carry the scenario as
their source, predicted ones their model; neither ever reaches the live line,
which holds only the scenario's record (a `TwinScenario`).

`create` checks a request, makes the branch and the record; `run` asks each
model through the predictor it is handed and writes the horizon; `compare`
reads a scenario beside the live line or another scenario; `get`, `list_all`
and `delete` do what they say. Predicting itself happens elsewhere: this
package imports no model library and never calls the worker.

A scenario with id `<id>` (twelve hexadecimal characters derived from what it
is) has the uid `twin_scenario:<id>` and the branch `scenario-<id>`.

Every refusal and failure is a `ScenarioError`. The statuses the API gives the
codes:

    scenario.bad_request        400   the request cannot be met; `detail.problems` lists
                                      every problem with the member it is about
    scenario.not_interventional 409   a control is changed and a model does not say its
                                      predictions hold under that
    scenario.not_found          404   no scenario of that uid
    scenario.running            409   it is being run
    scenario.already_run        409   it has been run
    scenario.deleted            410   it was deleted
    scenario.timeout            504   the budget ran out before every model had answered
    scenario.bad_prediction     502   a predictor answered with something that cannot be
                                      written

Errors of the store (`store.*`) and those a predictor raises pass through with
their own codes.
"""

from .comparison import compare
from .errors import (
    ALREADY_RUN,
    BAD_PREDICTION,
    BAD_REQUEST,
    DELETED,
    NOT_FOUND,
    NOT_INTERVENTIONAL,
    RUNNING,
    TIMEOUT,
    ScenarioError,
)
from .ops import create, delete, get, list_all, run
from .request import Predictor, ScenarioRequest

__all__ = [
    "ALREADY_RUN",
    "BAD_PREDICTION",
    "BAD_REQUEST",
    "DELETED",
    "NOT_FOUND",
    "NOT_INTERVENTIONAL",
    "RUNNING",
    "TIMEOUT",
    "Predictor",
    "ScenarioError",
    "ScenarioRequest",
    "compare",
    "create",
    "delete",
    "get",
    "list_all",
    "run",
]
