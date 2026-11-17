"""The functions of a twin: how it answers a question.

Seven are shipped with the runtime and parameterised by the manifest
(`dta_core.functions.BUILTINS` is their table): `latest`, `below`, `above`,
`integrate`, `paths`, `aggregate`, `series`, and `forecast`, which is handed to
whoever holds the models. `resolve` turns the manifest's fixed parameters and
those of one call into the values a built-in takes; `run` computes the answer at
the version the context pins. Code a twin brings is run by the worker through
`dta_runtime.functions.registered`.

Every refusal is a `FunctionError` whose `detail` names the function and the
parameter. The statuses the API gives the codes:

    function.param_unknown   400   a parameter the entry does not have
    function.param_fixed     400   fixed by the manifest and given with the call
    function.param_missing   400   required, and given in neither place
    function.bad_param       400   a value of the wrong shape, or one the ontology refuses
    function.bad_unit        400   a unit the property's cannot be converted to
    function.timeout         504   the deadline passed
    function.unavailable     503   a forecast was asked for and nothing can make one

Errors of the store and of the graph (`store.unknown_type`,
`store.unknown_property`, `store.unknown_version`, `graph.too_broad`) pass
through with their own codes.

The names below are loaded on first use: the worker imports
`dta_runtime.functions.registered` in a fresh process for every run of a twin's
own code, which must not pay for the store and the graph it never touches.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .errors import FunctionError

if TYPE_CHECKING:
    from .context import Context
    from .params import resolve
    from .runner import run

_LAZY = {"Context": ".context", "resolve": ".params", "run": ".runner"}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    found = getattr(import_module(module, __name__), name)
    globals()[name] = found
    return found


__all__ = ["Context", "FunctionError", "resolve", "run"]
