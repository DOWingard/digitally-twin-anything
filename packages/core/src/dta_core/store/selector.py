"""Text form naming one version of a branch, or a run of them.

head                         the newest version
<commit id>                  that version
#<seq>                       the version at that position
<ISO 8601 time with offset>  the last version whose time is not later
<a>..<b>                     every version from a to b, both included
<a>..<b>/<step>              every step-th version, starting at a
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .errors import InvalidInput, invalid
from .model import iso_utc, to_utc

COMMIT_ID = re.compile(r"^[0-9a-v]{32}$")
_SEQ = re.compile(r"^#(\d{1,12})$")
_STEP = re.compile(r"^[1-9]\d{0,8}$")


@dataclass(frozen=True)
class Selector:
    kind: str  # "head" | "id" | "seq" | "time" | "range"
    id: str | None = None
    seq: int | None = None
    time: datetime | None = None
    start: Selector | None = None
    end: Selector | None = None
    step: int = 1

    @property
    def is_range(self) -> bool:
        return self.kind == "range"

    @classmethod
    def parse(cls, text: str) -> Selector:
        if not isinstance(text, str) or not text.strip():
            raise invalid("bad_selector", f"a version selector must be text, got {text!r}")
        text = text.strip()
        if ".." not in text:
            if "/" in text:
                raise invalid("bad_selector", f"a step needs a range '<a>..<b>': {text!r}")
            return cls._single(text)
        body, slash, step_text = text.partition("/")
        step = 1
        if slash:
            if not _STEP.match(step_text):
                raise invalid(
                    "bad_selector", f"the step of a range must be a positive integer: {text!r}"
                )
            step = int(step_text)
        parts = body.split("..")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise invalid("bad_selector", f"a range is written '<a>..<b>': {text!r}")
        return cls("range", start=cls._single(parts[0]), end=cls._single(parts[1]), step=step)

    @classmethod
    def _single(cls, text: str) -> Selector:
        text = text.strip()
        if text == "head":
            return cls("head")
        if COMMIT_ID.match(text):
            return cls("id", id=text)
        match = _SEQ.match(text)
        if match:
            return cls("seq", seq=int(match.group(1)))
        if text[:1].isdigit():
            try:
                return cls("time", time=to_utc(text, f"selector {text!r}"))
            except InvalidInput as exc:
                # A time without an offset is refused as such, not as an unknown form.
                if exc.code == "store.naive_datetime":
                    raise
        raise invalid(
            "bad_selector",
            f"not a version selector: {text!r} (use head, a commit id, #<seq>, "
            "an ISO 8601 time with an offset, or <a>..<b>[/<step>])",
        )

    def __str__(self) -> str:
        if self.kind == "head":
            return "head"
        if self.kind == "id":
            return str(self.id)
        if self.kind == "seq":
            return f"#{self.seq}"
        if self.kind == "time":
            return iso_utc(self.time) if self.time else ""
        text = f"{self.start}..{self.end}"
        return text if self.step == 1 else f"{text}/{self.step}"
