"""Commands attached directly to `dta` (everything except `dta tasks`)."""

from __future__ import annotations

import typer

from . import (
    env,
    init,
    lifecycle,
    manifest,
    ontology,
    profile,
    sign,
    skill,
    source,
    status,
)


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

    env_app = typer.Typer(add_completion=False, no_args_is_help=True)
    env_app.command("template")(env.template)
    env_app.command("check")(env.check)
    app.add_typer(env_app, name="env", help="The variables of the twin's stack, in .env.")

    source_app = typer.Typer(add_completion=False, no_args_is_help=True)
    source_app.command("probe")(source.probe)
    app.add_typer(source_app, name="source", help="Reach the twin's sources from its stack.")

    app.command("profile")(profile.profile)

    ontology_app = typer.Typer(add_completion=False, no_args_is_help=True)
    ontology_app.command("check")(ontology.check)
    app.add_typer(ontology_app, name="ontology", help="The ontology file and its confirmed types.")

    app.command("up")(lifecycle.up)
    app.command("down")(lifecycle.down)
    app.command("logs")(lifecycle.logs)

    generate_app = typer.Typer(add_completion=False, no_args_is_help=True)
    generate_app.command("skill")(skill.generate_skill)
    app.add_typer(generate_app, name="generate", help="Render what the twin hands over.")
    app.command("install-skill")(skill.install_skill)
