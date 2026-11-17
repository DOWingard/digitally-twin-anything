"""Commands attached directly to `dta` (everything except `dta tasks`)."""

from __future__ import annotations

import typer

from . import (
    accept,
    build,
    compute,
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
    sync,
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

    sync_app = typer.Typer(add_completion=False, no_args_is_help=True)
    sync_app.command("derive")(sync.derive)
    sync_app.command("run")(sync.run_sync)
    sync_app.command("status")(sync.sync_status)
    app.add_typer(
        sync_app, name="sync", help="How often the twin reads its sources, and its sync runs."
    )

    ontology_app = typer.Typer(add_completion=False, no_args_is_help=True)
    ontology_app.command("check")(ontology.check)
    app.add_typer(ontology_app, name="ontology", help="The ontology file and its confirmed types.")

    app.command("up")(lifecycle.up)
    app.command("down")(lifecycle.down)
    app.command("logs")(lifecycle.logs)
    app.command("build")(build.build)
    app.command("accept")(accept.accept)

    app.command("register")(compute.register)
    models_app = typer.Typer(add_completion=False, no_args_is_help=True)
    models_app.command("evaluate")(compute.models_evaluate)
    models_app.command("list")(compute.models_list)
    app.add_typer(models_app, name="models", help="The twin's models: evaluate and list them.")
    questions_app = typer.Typer(add_completion=False, no_args_is_help=True)
    questions_app.command("list")(compute.questions_list)
    questions_app.command("measure")(compute.questions_measure)
    questions_app.command("answer")(compute.questions_answer)
    app.add_typer(
        questions_app, name="questions", help="The twin's questions: list, measure and ask them."
    )
    scenarios_app = typer.Typer(add_completion=False, no_args_is_help=True)
    scenarios_app.command("list")(compute.scenarios_list)
    app.add_typer(scenarios_app, name="scenarios", help="The twin's what-if scenarios.")

    generate_app = typer.Typer(add_completion=False, no_args_is_help=True)
    generate_app.command("skill")(skill.generate_skill)
    app.add_typer(generate_app, name="generate", help="Render what the twin hands over.")
    app.command("install-skill")(skill.install_skill)
