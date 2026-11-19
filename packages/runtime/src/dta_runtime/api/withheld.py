"""Refusing what would compute on an unknown value.

A suspect signal whose inferred value ran out (past the inferring question's horizon,
not finite, or outside the property's range) leaves its property without a value for
that object until it clears. A model, a function or a question that reads such a value
is refused before it runs, with `fidelity.input_unknown` and the withheld signals as the
problem's `signals` (`{signal, property, object, since, why}` each).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from dta_core.functions import BUILTINS, ParamKind
from dta_core.log import Log
from dta_core.store import VersionRef

from .. import fidelity
from ..fidelity.errors import INPUT_UNKNOWN
from .problems import ApiError

if TYPE_CHECKING:
    from .services import Services

log = Log("runtime.api.withheld")


def refuse_unknown(
    services: Services,
    version: VersionRef,
    properties: Iterable[str] | None,
    *,
    uids: Sequence[str] | None = None,
    subject: Mapping[str, Any],
) -> None:
    """Raise `fidelity.input_unknown` when any of `properties` (`Type.property`; an
    abstract type stands for its concrete types; None for every property, as code of
    the twin's own that declares no `reads` is taken to read), of the objects `uids`
    when given, is unknown at `version`. `subject` names what was refused, as the
    problem's detail carries it."""
    wanted = None if properties is None else [text for text in properties if text]
    if wanted is not None and not wanted:
        return
    cells = fidelity.unknown(services.twin, version)
    if not cells:
        return
    ontology = services.ontology(version)
    hits = fidelity.withheld(cells, wanted, uids=uids, concrete=ontology.abstract)
    if not hits:
        log.debug("no input is unknown", **subject, unknown=len(cells))
        return
    names = ", ".join(hit["signal"] for hit in hits)
    log.info(
        "refused: an input is unknown",
        **subject,
        version=version.id,
        signals=[hit["signal"] for hit in hits],
    )
    raise ApiError(
        f"the value of {names} is unknown: the signal is suspect and nothing is inferred "
        "for it any more; nothing is computed from it until it clears",
        code=INPUT_UNKNOWN,
        detail={**subject, "signals": hits},
        extra={"signals": hits},
    )


def function_inputs(entry: str, params: Mapping[str, Any]) -> tuple[list[str], list[str] | None]:
    """The properties a built-in function reads (its parameters of kind `property`) and
    the objects it reads them of (its `uids`, every object when absent)."""
    builtin = BUILTINS.get(entry)
    if builtin is None:
        return [], None
    properties = [
        str(params[parameter.name])
        for parameter in builtin.parameters
        if parameter.kind == ParamKind.property and params.get(parameter.name)
    ]
    uids = params.get("uids")
    return properties, [str(uid) for uid in uids] if uids else None
