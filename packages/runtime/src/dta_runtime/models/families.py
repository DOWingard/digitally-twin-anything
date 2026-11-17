"""Making the family a request names: a shipped adapter, or code of the twin's own."""

from __future__ import annotations

import importlib
from datetime import timedelta

from dta_core.log import Log

from ..protocol.answers import Reader
from ..protocol.families import Family
from ..protocol.worker import FamilyRef
from .errors import UNKNOWN_FAMILY, ModelError, bad_family
from .shipped import SHIPPED

log = Log("models.families")


def label(family: FamilyRef) -> str:
    """How a family is called in logs and messages."""
    return family.name if family.name is not None else str(family.entry)


def make(family: FamilyRef, *, step: timedelta, twin: Reader) -> Family:
    """The family, made for one run on a grid of this step.

    Code of the twin's own is on `sys.path` already (the worker put it there): its
    entry is called with the step and a reader of the twin, and what it gives back has
    to be a family.
    """
    if family.name is not None:
        shipped = SHIPPED.get(family.name)
        if shipped is None:
            raise ModelError(
                f"unknown family {family.name!r}; the shipped families are {', '.join(SHIPPED)}",
                code=UNKNOWN_FAMILY,
                detail={"family": family.name},
            )
        return shipped(step=step)
    entry = str(family.entry)
    module_name, _, name = entry.partition(":")
    try:
        factory = getattr(importlib.import_module(module_name), name)
        made = factory(step=step, twin=twin)
    except Exception as exc:
        log.error("a family of the twin's own could not be made", exc=exc, entry=entry)
        raise bad_family(
            f"the family {entry} could not be made ({type(exc).__name__}); the cause is in the log",
            entry=entry,
            error=type(exc).__name__,
        ) from exc
    if not isinstance(made, Family):
        missing = [
            member
            for member in ("interventional", "fit", "predict", "validity")
            if not hasattr(made, member)
        ]
        log.error("the twin's own code did not give a family", entry=entry, missing=missing)
        raise bad_family(
            f"{entry} gave a {type(made).__name__}, which is not a family: it lacks "
            f"{', '.join(missing)}",
            entry=entry,
            missing=missing,
        )
    return made
