"""Small pieces the tabs share: layout parts, and what a selected version names."""

from __future__ import annotations

from typing import Any

from dash import html

# The branch of the live twin; any other branch holds a scenario.
LIVE_BRANCH = "main"


def field(label: str, control: Any) -> html.Div:
    """A filter control with its label above it."""
    return html.Div([html.Label(label, htmlFor=control.id), control], className="field")


def branch_of(version: dict[str, Any] | None) -> str:
    """The branch a selected version is on: every read at it names that branch too."""
    return str((version or {}).get("branch") or LIVE_BRANCH)


def at_version(version: dict[str, Any]) -> dict[str, str]:
    """The query that reads at a selected version: its id, on its branch."""
    return {"at": str(version["id"]), "branch": branch_of(version)}
