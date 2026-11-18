"""`dta ontology check`: the ontology file's type signature against the confirmed one.

`ontology.type_signature` holds the fingerprint of the object and link types
the user confirmed. Writing a new one resets its status to proposed, so a
change of the types always goes back to the user.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer
from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.manifest import Status
from dta_core.manifest.models import OntologyRef
from dta_core.ontology import OntologyError, load_ontology, type_signature
from dta_core.schema import META_CLASS_PREFIX, Ontology

from ..output import command, emit, session
from .common import ONTOLOGY_UNUSABLE, fail_check, ontology_path

log = Log("commands.ontology")

SIGNATURE_PATH = "ontology.type_signature"
SIGNATURE_DIFFERS = "ontology.signature"


def describe_types(ontology: Ontology) -> dict[str, Any]:
    """The domain's types as a reviewer sees them: objects, links and abstract classes."""
    objects, links = [], []
    for object_type in ontology.domain_types():
        if object_type.link is not None:
            source, target = object_type.link
            links.append(
                {
                    "name": object_type.name,
                    "source_type": object_type.field(source).target,
                    "target_type": object_type.field(target).target,
                    "properties": len(object_type.fields),
                }
            )
        else:
            objects.append({"name": object_type.name, "properties": len(object_type.fields)})
    abstract = [
        {
            "name": name,
            "members": list(members),
            # Properties every concrete member has, which is what the class itself promises.
            "properties": len(
                set.intersection(
                    *({f.name for f in ontology.type(member).fields} for member in members)
                )
            )
            if members
            else 0,
        }
        for name, members in sorted(ontology.abstract.items())
        if not name.startswith(META_CLASS_PREFIX)
    ]
    return {"objects": objects, "links": links, "abstract": abstract}


def _text(result: dict[str, Any]) -> str:
    types = result["types"]
    lines = [
        f"File:     {result['path']}",
        f"Types:    {len(types['objects'])} object, {len(types['links'])} link, "
        f"{len(types['abstract'])} abstract",
    ]
    for entry in types["objects"]:
        lines.append(f"  object   {entry['name']} ({entry['properties']} properties)")
    for entry in types["links"]:
        lines.append(
            f"  link     {entry['name']}: {entry['source_type']} -> {entry['target_type']} "
            f"({entry['properties']} properties)"
        )
    for entry in types["abstract"]:
        lines.append(f"  abstract {entry['name']}: {', '.join(entry['members'])}")
    lines += [
        f"File signature:     {result['file_signature']}",
        f"Manifest signature: {result['manifest_signature'] or 'not set'}",
    ]
    if result["agree"]:
        lines.append("The signatures agree.")
    elif result["written"]:
        lines.append(f"Wrote the file's signature to {SIGNATURE_PATH}; confirm the types again.")
    else:
        lines.append("The signatures differ; run with --write to propose the file's types.")
    return "\n".join(lines)


@command
def check(
    ctx: typer.Context,
    write: Annotated[
        bool,
        typer.Option("--write", help="Write the file's signature to the manifest when it differs."),
    ] = False,
) -> None:
    """Compare the ontology file's type signature with ontology.type_signature of the
    manifest, whether that is confirmed or only proposed."""
    workspace = session(ctx).workspace
    manifest = workspace.load_manifest()
    path = ontology_path(workspace, manifest)
    if path is None:
        log.error("the manifest names no ontology file")
        raise DtaError(
            "the manifest names no ontology file (ontology.path)", code="ontology.no_path"
        )
    relative = (
        str(path.relative_to(workspace.root)) if workspace.root in path.parents else str(path)
    )
    if not path.is_file():
        log.error("ontology file is missing", path=relative)
        raise DtaError(
            f"the ontology file {relative} does not exist",
            code="ontology.missing",
            detail={"path": relative},
        )
    try:
        ontology = load_ontology(path)
    except OntologyError as exc:
        log.warn("ontology file cannot be used", path=relative, code=exc.code)
        problems = exc.detail.get("problems") or []
        result = {
            "path": relative,
            "loader_code": exc.code,
            "message": exc.message,
            "problems": problems,
        }
        lines = [f"{relative} cannot be used: {exc.message}"]
        lines += [f"  {p.get('rule')} {p.get('class') or ''} {p.get('message')}" for p in problems]
        fail_check(ctx, ONTOLOGY_UNUSABLE, f"{relative} cannot be used", result, "\n".join(lines))

    signature = type_signature(ontology)
    confirmed = manifest.ontology.type_signature if manifest.ontology else None
    agree = confirmed == signature
    written = False
    if not agree and write:
        with workspace.exclusive():
            current = workspace.load_manifest()
            reference = current.ontology or OntologyRef()
            reference.type_signature = signature
            current.ontology = reference
            statuses = dict(current.field_status)
            statuses[SIGNATURE_PATH] = Status.proposed
            current.field_status = dict(sorted(statuses.items()))
            workspace.save_manifest(current)
        written = True
        log.info("type signature proposed", path=relative, signature=signature, was=confirmed)
    result = {
        "path": relative,
        "file_signature": signature,
        "manifest_signature": confirmed,
        "agree": agree,
        "written": written,
        "types": describe_types(ontology),
    }
    text = _text(result)
    if not agree and not write:
        log.warn("type signatures differ", file=signature, manifest=confirmed)
        fail_check(
            ctx,
            SIGNATURE_DIFFERS,
            "the ontology file's types differ from the confirmed ones",
            result,
            text,
        )
    emit(ctx, result, text)
