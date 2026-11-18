"""`dta generate sdk`: the twin's typed Python client, from its ontology and its running API."""

from __future__ import annotations

from typing import Annotated, Any

import typer
from dta_core.errors import CheckFailed, DtaError
from dta_core.log import Log

from .. import addons
from ..acceptance import TwinApi
from ..output import command, emit, session
from ..sdk import ensure_sdk, marker_path
from ..stack import Stack
from .common import ontology_path, require_complete_env, timed

log = Log("commands.sdk")

OPENAPI_PATH = "/api/v1/openapi.json"


def _text(result: dict[str, Any]) -> str:
    if result["action"] == "current":
        return f"{result['path']} is current ({result['package']}); nothing generated."
    lines = [
        f"Generated {result['package']} into {result['path']} ({result['files']} files, "
        f"{result['timings_ms']['total_ms'] / 1000:.1f} s).",
        "Install it with: uv pip install " + result["path"],
    ]
    lines += [f"warning: {text}" for text in result.get("warnings") or []]
    return "\n".join(lines)


@command
def generate_sdk(
    ctx: typer.Context,
    force: Annotated[
        bool, typer.Option("--force", help="Generate even when nothing it is made from changed.")
    ] = False,
) -> None:
    """Generate the typed Python client into sdk/python/; needs the running API."""
    workspace = session(ctx).workspace
    manifest = workspace.load_manifest()
    if not addons.selected(manifest).sdk_python:
        log.warn("client generation refused: the add-on is off")
        raise CheckFailed(
            "contract.addons.sdk_python is false: no client is built for this twin",
            code="sdk.not_selected",
            detail={"field": "contract.addons.sdk_python"},
        )
    path = ontology_path(workspace, manifest)
    if path is None or not path.is_file():
        raise DtaError("the manifest's ontology file is missing", code="ontology.missing")
    values = require_complete_env(Stack(workspace, manifest))
    with timed(log, "client generation") as extra:
        with TwinApi(manifest, values) as client:
            response = client.get(OPENAPI_PATH)
        if response.status_code != 200:
            log.error("the API did not give its document", status=response.status_code)
            raise DtaError(
                f"GET {OPENAPI_PATH} answered {response.status_code}; is the twin up (dta up)?",
                code="sdk.api_unavailable",
                detail={"status": response.status_code},
            )
        if force:
            with workspace.exclusive():
                workspace.remove(marker_path(workspace))
        result = ensure_sdk(workspace, manifest, path, response.json())
        extra["action"] = result["action"]
    emit(ctx, result, _text(result))
