"""The file of rows a run set aside: which row, which rule, never the row's values."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from dta_core.log import Log
from dta_core.schema import Violation
from dta_core.store.model import iso_utc

from .errors import REJECTS_UNWRITABLE, SyncError

log = Log("sync.rejects")

REJECTS_DIR = "rejects"
RULE_UID = "uid"
RULE_TYPE = "type"
RULE_VALID_TIME = "valid_time"
RULE_DUPLICATE = "duplicate"
RULE_RANGE = "range"

# What is said about a broken rule. The store's own messages may quote the
# offending value, so they are never copied into the file.
_MESSAGES = {
    "type": "the value does not fit the property's kind",
    "required": "a required property is missing or null",
    "enum": "the value is not one of the enum's values",
    "range": "the value is outside the property's bounds",
    "pattern": "the value does not match the property's pattern",
    "length": "the text is too long",
    "check": "a check of the ontology does not hold",
    "uid": "the identity does not give a usable uid",
    "uid_type": "the uid already belongs to an object of another type",
    "duplicate": "the object is written twice in one version",
    "referenced": "the object is still referred to and cannot be deleted",
    "not_found": "the object to delete does not exist",
    "unknown_property": "the type has no such property",
    "fact_kind": "facts of this kind cannot be written by sync",
    "provenance": "the row carries no usable provenance",
    "database": "the database refused the row",
}
_CHECK_NAME = re.compile(r'Check constraint "[A-Za-z0-9_]*?__chk_([A-Za-z0-9_]+)"')


def describe(violation: Violation) -> str:
    """A message for one violation that names the rule and nothing of the row."""
    if violation.rule == "reference":
        if "does not exist" in violation.message:
            return "refers to an object that does not exist"
        return "refers to an object of a type the property does not allow"
    if violation.rule == "check":
        named = _CHECK_NAME.search(violation.message)
        if named:
            return f"the ontology's check {named.group(1)} does not hold"
    return _MESSAGES.get(violation.rule, f"rule {violation.rule} is broken")


class Rejects:
    """Appends one JSON line per row set aside; the file appears with the first line."""

    def __init__(self, artifacts_dir: Path, run_key: str, run_uid: str) -> None:
        self._path = artifacts_dir / REJECTS_DIR / f"{run_key}.jsonl"
        self.location = f"{REJECTS_DIR}/{run_key}.jsonl"
        self._run = run_uid
        self._file: IO[str] | None = None
        self._started = False
        self.lines = 0
        # Lines written, by the rule that was broken.
        self.rules: Counter[str] = Counter()

    @property
    def written(self) -> bool:
        return self.lines > 0

    def add(
        self,
        *,
        mapping: str,
        source: str,
        type_name: str,
        uid: str | None,
        prop: str | None,
        rule: str,
        message: str,
        t: datetime | None = None,
        also: list[dict[str, Any]] | None = None,
    ) -> None:
        line = {
            "run": self._run,
            "source": source,
            "mapping": mapping,
            "type": type_name,
            "uid": uid,
            "property": prop,
            "rule": rule,
            "message": message,
            # Valid time of the row or, when a version was refused, that version's time.
            "t": iso_utc(t) if t is not None else None,
        }
        if also:
            line["also"] = also
        try:
            if self._file is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                # A run that wrote nothing can repeat under the same key; it starts over.
                self._file = self._path.open("a" if self._started else "w", encoding="utf-8")
                self._started = True
            self._file.write(json.dumps(line, separators=(",", ":")) + "\n")
        except OSError as exc:
            log.error(
                "the rejects file cannot be written; the run stops", exc=exc, path=str(self._path)
            )
            raise SyncError(
                f"rows set aside cannot be recorded in {self.location}: {type(exc).__name__}",
                code=REJECTS_UNWRITABLE,
                detail={"location": self.location},
            ) from exc
        self.lines += 1
        self.rules[rule] += 1

    def add_violations(
        self,
        *,
        mapping: str,
        source: str,
        type_name: str,
        uid: str,
        violations: list[Violation],
        t: datetime,
    ) -> None:
        first, rest = violations[0], violations[1:]
        self.add(
            mapping=mapping,
            source=source,
            type_name=type_name,
            uid=uid,
            prop=first.property,
            rule=first.rule,
            message=describe(first),
            t=t,
            also=[{"property": v.property, "rule": v.rule, "message": describe(v)} for v in rest],
        )

    def flush(self) -> None:
        if self._file is not None:
            try:
                self._file.flush()
            except OSError as exc:
                log.warn("the rejects file could not be flushed", error=repr(exc))

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError as exc:
                log.warn("the rejects file could not be closed", error=repr(exc))
            self._file = None
