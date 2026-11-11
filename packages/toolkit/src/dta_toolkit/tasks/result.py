"""A task's result file and how its manifest patch is applied.

`output/result.json` is the only channel from a task into shared state. Its
`manifest_patch` sets values at dotted manifest paths, all of which must lie
inside the keys the task owns. Anything else the task wants is a proposal: it
is stored for the orchestrator and never applied.

Setting a value is idempotent, so applying the same patch a second time leaves
the manifest unchanged. The merge relies on this to recover from an
interruption.
"""

from __future__ import annotations

import json
from typing import Any

from dta_core.errors import ManifestError
from dta_core.manifest import Manifest, parse_manifest, to_plain
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .catalogue import TaskSpec
from .errors import reason
from .paths import covers, is_path, parse_pattern


class Proposal(BaseModel):
    """A change the task wants but may not make itself. Further keys are kept as given."""

    model_config = ConfigDict(extra="allow")

    target: str = Field(min_length=1)  # what it concerns: "ontology", a manifest path, ...
    description: str = Field(min_length=1)


class Result(BaseModel):
    """Every key is required, so that "no patch" is something the task states, not an omission."""

    model_config = ConfigDict(extra="forbid")

    manifest_patch: dict[str, Any]
    proposals: list[Proposal]
    artifacts: list[str]
    notes: str


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"key {key!r} appears twice")
        seen[key] = value
    return seen


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON value")


def parse_result(raw: bytes) -> tuple[Result | None, list[dict[str, Any]]]:
    """Decode a result file. Returns the result, or the reasons it is not acceptable."""
    try:
        data = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constants
        )
    except (UnicodeDecodeError, ValueError) as exc:
        return None, [reason("tasks.result.invalid", f"result.json is not valid JSON: {exc}")]
    try:
        return Result.model_validate(data), []
    except ValidationError as exc:
        problems = [
            {"path": ".".join(str(part) for part in error["loc"]), "problem": error["msg"]}
            for error in exc.errors()
        ]
        return None, [
            reason(
                "tasks.result.invalid",
                "result.json must be an object with exactly the keys manifest_patch, "
                "proposals, artifacts and notes",
                problems=problems,
            )
        ]


def ownership_reasons(spec: TaskSpec, patch: dict[str, Any]) -> list[dict[str, Any]]:
    """Patch keys that are malformed or outside what the task owns."""
    owned = [parse_pattern(text) for text in spec.owns]
    malformed = sorted(key for key in patch if not is_path(key))
    outside = sorted(
        key
        for key in patch
        if is_path(key) and not any(covers(pattern, key.split(".")) for pattern in owned)
    )
    reasons = []
    if malformed:
        reasons.append(
            reason(
                "tasks.patch.key",
                "manifest_patch keys must be dotted manifest paths",
                keys=malformed,
            )
        )
    if outside:
        reasons.append(
            reason(
                "tasks.patch.not_owned",
                "manifest_patch sets keys the task does not own; describe such changes "
                "in proposals instead",
                keys=outside,
                owned_keys=list(spec.owns),
            )
        )
    return reasons


def artifact_reasons(declared: list[str], present: set[str]) -> list[dict[str, Any]]:
    """Compare the files a result declares with the files actually in the output directory.

    The holder vouches for every file it hands over. A file nobody declared was
    not necessarily written by the holder, so it is refused rather than merged.
    """
    reasons = []
    bad = sorted(
        {
            path
            for path in declared
            if path == ""
            or path.startswith("/")
            or "\\" in path
            or "\x00" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
        }
    )
    if bad:
        reasons.append(
            reason(
                "tasks.artifact.path",
                "artifacts must be relative paths inside the output directory",
                paths=bad,
            )
        )
    listed = set(declared) - set(bad)
    missing = sorted(listed - present)
    undeclared = sorted(present - listed)
    if missing:
        reasons.append(
            reason(
                "tasks.artifact.missing",
                "artifacts lists files that are not in the output directory",
                paths=missing,
            )
        )
    if undeclared:
        reasons.append(
            reason(
                "tasks.artifact.undeclared",
                "the output directory holds files that artifacts does not list; "
                "list them or remove them",
                paths=undeclared,
            )
        )
    return reasons


def apply_patch(manifest: Manifest, patch: dict[str, Any]) -> Manifest:
    """Set each patched path and return the manifest that results, checked against the schema.

    Raises `ManifestError` when a path runs through something that is not a
    mapping or when the result no longer matches the schema.
    """
    plain = to_plain(manifest)
    # A shorter path is set before the paths below it, whatever order the file used.
    for key in sorted(patch, key=lambda text: text.split(".")):
        value = patch[key]
        parts = key.split(".")
        node: dict[str, Any] = plain
        blocked = False
        for depth, part in enumerate(parts[:-1]):
            child = node.get(part)
            if child is None:
                if value is None:
                    blocked = True  # nothing there to remove
                    break
                child = {}
                node[part] = child
            elif not isinstance(child, dict):
                raise ManifestError(
                    f"cannot set {key}: {'.'.join(parts[: depth + 1])} is not a mapping",
                    code="manifest.schema",
                    detail={
                        "problems": [
                            {"path": ".".join(parts[: depth + 1]), "problem": "not a mapping"}
                        ]
                    },
                )
            node = child
        if blocked:
            continue
        if value is None:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = value
    return parse_manifest(plain, origin="the manifest with the task's patch applied")


def _changed(before: Any, after: Any, path: tuple[str, ...]) -> list[tuple[str, ...]]:
    """Paths at which two plain manifests differ, as deep as the difference can be located."""
    if before == after:
        return []
    if isinstance(after, dict) and after and before is None:
        before = {}
    if isinstance(before, dict) and before and after is None:
        after = {}
    if isinstance(before, dict) and isinstance(after, dict):
        found = []
        for key in sorted(set(before) | set(after)):
            found += _changed(before.get(key), after.get(key), (*path, str(key)))
        return found
    return [path]


def side_effect_reasons(spec: TaskSpec, before: Manifest, after: Manifest) -> list[dict[str, Any]]:
    """Changes a patch would cause outside the keys the task owns.

    Every patch key may be owned and the result may still differ elsewhere:
    creating an entry in order to set one of its fields also gives the entry's
    other fields their default values. Comparing the two manifests is the check
    that does not depend on how the patch was written.
    """
    owned = [parse_pattern(text) for text in spec.owns]
    strays = sorted(
        ".".join(path)
        for path in _changed(to_plain(before), to_plain(after), ())
        if not any(covers(pattern, path) for pattern in owned)
    )
    if not strays:
        return []
    return [
        reason(
            "tasks.patch.side_effect",
            "applying this patch would also change manifest values the task does not own",
            paths=strays,
            owned_keys=list(spec.owns),
        )
    ]
