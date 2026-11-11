"""The outcome of one acceptance check, as it is reported."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class CheckResult:
    id: str
    examined: str  # what was looked at, in a sentence
    passed: bool
    evidence: dict[str, Any] = field(default_factory=dict)
    remedy: str | None = None  # what to change when it failed
    duration_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "examined": self.examined,
            "passed": self.passed,
            "evidence": self.evidence,
            "remedy": None if self.passed else self.remedy,
            "duration_ms": self.duration_ms,
        }


def problem_of(response: httpx.Response) -> dict[str, Any]:
    """Status and, for an RFC 9457 problem document, its code and title."""
    found: dict[str, Any] = {"status": response.status_code}
    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            return found
        if isinstance(body, dict):
            found.update({key: body.get(key) for key in ("code", "title", "detail") if key in body})
    return found


def described(method: str, path: str, params: dict[str, Any] | None = None) -> str:
    """A request as reports show it: method, path and query, never a credential."""
    if not params:
        return f"{method} {path}"
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"{method} {path}?{query}"
