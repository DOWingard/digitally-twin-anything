"""Small pieces of layout the tabs share."""

from __future__ import annotations

from typing import Any

from dash import html


def field(label: str, control: Any) -> html.Div:
    """A filter control with its label above it."""
    return html.Div([html.Label(label, htmlFor=control.id), control], className="field")
