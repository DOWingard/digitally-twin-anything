"""Commands attached directly to `dta` (everything except `dta tasks`)."""

from __future__ import annotations

import typer


def register(app: typer.Typer) -> None:
    """Attach this package's commands and sub-apps to the root application."""
