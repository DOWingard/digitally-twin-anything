"""The one-page contract summary the user reads before signing.

The rendering is a pure function of the manifest and the ontology's type names:
no timestamps, every collection sorted, so the same manifest always gives the
same bytes and the summary can be hashed into a signature.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from ...errors import DtaError
from ...log import Log
from ..io import contract_hash, manifest_hash
from ..models import Bounds, Manifest, Tolerance
from .view import fmt_duration

log = Log("manifest.summary")

_UNSET = "not set"
_LINK_ANNOTATIONS = ("link_source", "link_target")


@dataclass(frozen=True)
class OntologyTypes:
    objects: tuple[str, ...]
    links: tuple[str, ...]


def read_ontology_types(path: Path) -> OntologyTypes:
    """Object and link type names declared in a LinkML file (imports not followed)."""
    # Imported lazily: the LinkML runtime is slow to load and only the summary needs it.
    from linkml_runtime.utils.schemaview import SchemaView

    started = time.perf_counter()
    try:
        classes = SchemaView(str(path)).all_classes(imports=False)
    except Exception as exc:
        log.error("ontology file cannot be read as LinkML", exc=exc, path=str(path))
        raise DtaError(
            f"{path} is not a readable LinkML schema",
            code="ontology.unreadable",
            detail={"path": str(path)},
        ) from exc
    objects, links = [], []
    for name, definition in classes.items():
        annotations = definition.annotations or {}
        if all(key in annotations for key in _LINK_ANNOTATIONS):
            links.append(str(name))
        else:
            objects.append(str(name))
    types = OntologyTypes(objects=tuple(sorted(objects)), links=tuple(sorted(links)))
    log.debug(
        "ontology types read",
        path=str(path),
        objects=len(objects),
        links=len(links),
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return types


def summary_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cell(value: object) -> str:
    text = _UNSET if value is None or value == "" else str(value)
    return " ".join(text.split()).replace("|", "\\|")


def _table(header: Sequence[str], rows: Iterable[Sequence[object]]) -> list[str]:
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    lines += ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows]
    return lines


def _number(value: float | None) -> str | None:
    return None if value is None else f"{value:g}"


def _duration(value: timedelta | None) -> str | None:
    return None if value is None else fmt_duration(value)


def _tolerance(tolerance: Tolerance | None) -> str | None:
    if tolerance is None or tolerance.value is None:
        return None
    metric = tolerance.metric.value if tolerance.metric else "error"
    unit = f" {tolerance.unit}" if tolerance.unit else ""
    return f"{metric} <= {tolerance.value:g}{unit}"


def _bounds(bounds: Bounds | None) -> str | None:
    if bounds is None:
        return None
    parts = []
    if bounds.minimum is not None or bounds.maximum is not None:
        low = _number(bounds.minimum) or "-inf"
        high = _number(bounds.maximum) or "+inf"
        parts.append(f"{low} to {high}")
    if bounds.allowed:
        parts.append("one of " + ", ".join(str(value) for value in bounds.allowed))
    if bounds.max_step is not None:
        parts.append(f"step <= {bounds.max_step:g}")
    return "; ".join(parts) or None


def _enum(value: object) -> str | None:
    return None if value is None else getattr(value, "value", str(value))


def render_summary(manifest: Manifest, ontology: OntologyTypes | None = None) -> str:
    twin = manifest.twin
    contract = manifest.contract
    name = twin.name if twin and twin.name else _UNSET
    twin_id = twin.id if twin and twin.id else _UNSET
    lines = [f"# Contract summary: {name} (`{twin_id}`)", ""]

    lines += _table(
        ("Field", "Value"),
        [
            ("Purpose", contract.purpose if contract else None),
            ("Tier", _enum(contract.tier) if contract else None),
        ],
    )

    questions = (contract.questions or {}) if contract else {}
    lines += ["", "## Questions", ""]
    if questions:
        ordered = sorted(questions.items(), key=lambda item: (item[1].priority or 0, item[0]))
        lines += _table(
            ("Id", "Question", "Kind", "Priority", "Tolerance", "Staleness", "Provisional"),
            [
                (
                    qid,
                    question.text,
                    _enum(question.kind),
                    question.priority,
                    _tolerance(question.tolerance),
                    _duration(question.staleness),
                    "yes" if question.provisional else "no",
                )
                for qid, question in ordered
            ],
        )
    else:
        lines.append("None.")

    controls = contract.controls if contract else {}
    lines += ["", "## Delegated controls", ""]
    if controls:
        lines += _table(
            ("Id", "Target", "Bounds", "Autonomy"),
            [
                (cid, control.target, _bounds(control.bounds), _enum(control.autonomy))
                for cid, control in sorted(controls.items())
            ],
        )
    else:
        lines.append("None.")

    objective = contract.objective if contract else None
    lines += ["", "## Objective", ""]
    if objective is not None:
        lines += _table(
            ("Description", "Direction"), [(objective.description, objective.direction)]
        )
        if objective.constraints:
            lines += [""]
            lines += _table(
                ("Constraint", "Description"),
                [
                    (cid, constraint.description)
                    for cid, constraint in sorted(objective.constraints.items())
                ],
            )
    else:
        lines.append("None.")

    sources = manifest.sources or {}
    lines += ["", "## Sources", ""]
    if sources:
        lines += _table(
            ("Id", "Description", "Connector", "Refresh period"),
            [
                (
                    sid,
                    source.description,
                    _enum(source.connector),
                    _duration(source.refresh_period),
                )
                for sid, source in sorted(sources.items())
            ],
        )
    else:
        lines.append("None.")
    sync_interval = manifest.sync.interval if manifest.sync else None
    lines += ["", f"Sync interval: {_duration(sync_interval) or _UNSET}"]

    lines += ["", "## Ontology", ""]
    path = manifest.ontology.path if manifest.ontology else None
    if ontology is not None:
        lines += _table(
            ("Kind", "Types"),
            [
                ("Object types", ", ".join(ontology.objects) or "none"),
                ("Link types", ", ".join(ontology.links) or "none"),
            ],
        )
    elif path:
        lines.append(f"Ontology file `{path}` is not written yet.")
    else:
        lines.append("No ontology file declared.")

    addons = contract.addons if contract else None
    enabled = (
        sorted(addon for addon, on in addons.model_dump().items() if on)
        if addons is not None
        else []
    )
    notification = contract.notification if contract else None
    if notification is None or notification.kind is None:
        notify = None
    elif notification.kind == "webhook":
        notify = f"webhook, address in `{notification.webhook_env or _UNSET}`"
    else:
        notify = notification.kind
    lines += ["", "## Add-ons and notification", ""]
    lines += _table(
        ("Setting", "Value"),
        [("Add-ons", ", ".join(enabled) or "none"), ("Notification", notify)],
    )

    lines += ["", "## Hashes", ""]
    lines += _table(
        ("Hash", "Value"),
        [
            ("Manifest", f"`{manifest_hash(manifest)}`"),
            ("Contract", f"`{contract_hash(manifest)}`"),
        ],
    )
    return "\n".join(lines) + "\n"
