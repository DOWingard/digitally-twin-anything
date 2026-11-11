"""Errors raised while reading an ontology or building its validators."""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import DtaError

INVALID = "ontology.invalid"
UNREADABLE = "ontology.unreadable"


@dataclass(frozen=True)
class Problem:
    """One way in which an ontology file breaks the conventions.

    `rule` is a stable code callers can branch on; `cls` and `slot` locate the
    problem (either may be absent for problems of the file as a whole).
    """

    rule: str
    cls: str | None
    slot: str | None
    message: str

    def as_dict(self) -> dict[str, str | None]:
        return {"rule": self.rule, "class": self.cls, "slot": self.slot, "message": self.message}


class OntologyError(DtaError):
    """The ontology file cannot be read or does not follow the conventions.

    With code `ontology.invalid`, `problems` lists every convention broken,
    all found in one pass; `detail["problems"]` holds the same as dictionaries.
    """

    code = INVALID

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        detail: dict | None = None,
        problems: tuple[Problem, ...] = (),
    ) -> None:
        detail = dict(detail or {})
        if problems:
            detail["problems"] = [p.as_dict() for p in problems]
        super().__init__(message, code=code, detail=detail)
        self.problems = problems

    def rules(self) -> set[str]:
        return {p.rule for p in self.problems}
