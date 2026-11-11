"""A task's result file and how its manifest patch is applied.

`output/result.json` is the only channel from a task into shared state. Its
`manifest_patch` sets values at dotted manifest paths, all of which must lie
inside the keys the task owns. Anything else the task wants is a proposal: it
is stored for the orchestrator and never applied.

Setting a value is idempotent, so applying the same patch a second time leaves
the manifest unchanged. The merge relies on this to recover from an
interruption.

A result is written by another agent and nothing in it is trusted. Before any
of it reaches a shared file it has to be shown that the file can still be read
back afterwards: text that cannot be encoded, values nested beyond what a
reader accepts, numbers that are not numbers.
"""

from __future__ import annotations

import json
import math
from typing import Any

import yaml
from dta_core.errors import DtaError, ManifestError
from dta_core.manifest import Manifest, dump_manifest, manifest_hash, parse_manifest, to_plain
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .catalogue import TaskSpec
from .errors import reason
from .paths import covers, is_path, parse_pattern

# Deeper than any manifest value has reason to be, and far below the depth at
# which JSON readers start to refuse a document.
MAX_DEPTH = 32
# Proposals and notes are copied into the state file, which every command reads.
MAX_RESULT_BYTES = 4 * 1024 * 1024


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


def _too_deep(data: Any) -> bool:
    pending = [(data, 1)]
    while pending:
        node, depth = pending.pop()
        if isinstance(node, dict | list):
            if depth > MAX_DEPTH:
                return True
            children = node.values() if isinstance(node, dict) else node
            pending.extend((child, depth + 1) for child in children)
    return False


def parse_result(raw: bytes) -> tuple[Result | None, list[dict[str, Any]]]:
    """Decode a result file. Returns the result, or the reasons it is not acceptable."""

    def invalid(message: str, **detail: Any) -> tuple[None, list[dict[str, Any]]]:
        return None, [reason("tasks.result.invalid", message, **detail)]

    try:
        data = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constants
        )
    except RecursionError:
        return invalid("result.json is nested too deeply to be read")
    except (UnicodeDecodeError, ValueError) as exc:
        return invalid(f"result.json is not valid JSON: {exc}")
    if _too_deep(data):
        return invalid(f"result.json nests values more than {MAX_DEPTH} levels deep")
    try:
        # An escape such as \udce9 is valid JSON but is half a character: text
        # carrying it cannot be written to a file again. A number such as 1e999
        # is read as infinity, which no later reader accepts.
        json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except UnicodeEncodeError:
        return invalid("result.json contains text that is not valid Unicode (a lone surrogate)")
    except ValueError:
        return invalid("result.json contains a number that is out of range")
    try:
        result = Result.model_validate(data)
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
    try:
        # What is kept of a result goes into the state file, which every later
        # command has to read: it must come back out exactly as it went in.
        stored = Result.model_validate_json(result.model_dump_json())
    except (ValueError, RecursionError) as exc:
        return invalid(f"result.json cannot be stored: {type(exc).__name__}")
    if stored != result:
        return invalid("result.json would not read back as written once stored")
    return result, []


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
    if isinstance(before, float) and isinstance(after, float):
        # A value that is not a number is not even equal to itself.
        return [] if math.isnan(before) and math.isnan(after) else [path]
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        found = []
        for index, (old, new) in enumerate(zip(before, after, strict=True)):
            found += _changed(old, new, (*path, str(index)))
        return [path] if found else []
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


def same_plain(first: Manifest, second: Manifest) -> bool:
    """True when two manifests hold the same values."""
    return not _changed(to_plain(first), to_plain(second), ())


def _finite(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except ValueError:
        return False
    return True


def stability_reasons(
    spec: TaskSpec, before: Manifest, patched: Manifest, patch: dict[str, Any]
) -> list[dict[str, Any]]:
    """Would the patched manifest survive being written, and the merge being repeated?

    Three things have to hold before a patch may touch the manifest file:
    the file it produces can be read back and holds the same values; applying
    the patch to what was read back changes nothing, which is what lets an
    interrupted merge be run again; and every number it brings in is a number.
    """
    problems: list[tuple[tuple[str, ...], str]] = []
    plain = to_plain(patched)
    for path in _changed(to_plain(before), plain, ()):
        node: Any = plain
        for part in path:
            node = node.get(part) if isinstance(node, dict) else None
        if not _finite(node):
            problems.append((path, "holds a value that is not a number"))
    try:
        manifest_hash(patched)
        reread = parse_manifest(
            yaml.safe_load(dump_manifest(patched)), origin="the manifest as it would be written"
        )
        manifest_hash(reread)
    except (DtaError, yaml.YAMLError, UnicodeError, ValueError, RecursionError) as exc:
        problems.append(((), f"cannot be written and read back: {type(exc).__name__}"))
    else:
        problems += [
            (path, "does not read back as written")
            for path in _changed(plain, to_plain(reread), ())
        ]
        try:
            again = apply_patch(reread, patch)
        except ManifestError:
            problems.append(((), "the patch cannot be applied a second time"))
        else:
            problems += [
                (path, "changes again when the patch is repeated")
                for path in _changed(to_plain(reread), to_plain(again), ())
            ]

    # A value elsewhere in the manifest that does not survive being rewritten
    # is not this task's doing, but merging would still alter it.
    owned = [parse_pattern(text) for text in spec.owns]

    def inside(path: tuple[str, ...]) -> bool:
        return not path or any(covers(pattern, path) for pattern in owned)

    mine = [{"path": ".".join(p), "problem": text} for p, text in problems if inside(p)]
    others = [{"path": ".".join(p), "problem": text} for p, text in problems if not inside(p)]
    reasons = []
    if mine:
        reasons.append(
            reason(
                "tasks.patch.unstable",
                "with this patch the manifest could not be written, read back and merged "
                "again without changing",
                problems=mine,
            )
        )
    if others:
        reasons.append(
            reason(
                "tasks.manifest.unstable",
                "the manifest holds values outside this task's keys that change when the "
                "file is rewritten, so no merge can leave them alone",
                problems=others,
            )
        )
    return reasons
