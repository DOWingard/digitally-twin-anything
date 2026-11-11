"""Commands attached directly to `dta` (everything except `dta tasks`)."""

from __future__ import annotations

import typer

from . import init, manifest, sign, status


def register(app: typer.Typer) -> None:
    """Attach this package's commands and sub-apps to the root application."""
    app.command("init")(init.init)
    app.command("status")(status.status)

    manifest_app = typer.Typer(add_completion=False, no_args_is_help=True)
    manifest_app.command("validate")(manifest.validate)
    manifest_app.command("gaps")(manifest.list_gaps)
    manifest_app.command("confirm")(manifest.confirm)
    manifest_app.command("summary")(manifest.summary)
    manifest_app.command("sign")(sign.sign)
    app.add_typer(
        manifest_app, name="manifest", help="Check, complete, summarise and sign the manifest."
    )
