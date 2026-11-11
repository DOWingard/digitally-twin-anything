"""Dotted manifest paths, the patterns that name owned keys, and when two of them overlap.

A path is a sequence of segments joined by dots: `sources.a.profile`. A pattern
is a path in which any segment may end in one `*`, which stands for any run of
characters inside that single segment: `functions.q1-*` names `functions.q1-kpi`
but not `functions.q1` and not `functions.q10`. A `*` never crosses a dot.

Everything is decided segment by segment, never on the raw string, so
`sources.a` and `sources.ab` are unrelated.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import TaskError

# Always applied with fullmatch: `$` would also accept a trailing newline.
_LITERAL = re.compile(r"[A-Za-z0-9_-]+")
_GLOB = re.compile(r"[A-Za-z0-9_-]*\*")


@dataclass(frozen=True)
class Segment:
    text: str  # the whole segment, or the fixed prefix when `glob` is set
    glob: bool = False

    def matches(self, literal: str) -> bool:
        return literal.startswith(self.text) if self.glob else literal == self.text

    def meets(self, other: Segment) -> bool:
        """True when some segment text satisfies both."""
        if self.glob and other.glob:
            return self.text.startswith(other.text) or other.text.startswith(self.text)
        if self.glob:
            return other.text.startswith(self.text)
        if other.glob:
            return self.text.startswith(other.text)
        return self.text == other.text


Pattern = tuple[Segment, ...]


def is_path(text: Any) -> bool:
    """True for a well-formed dotted path without wildcards."""
    return (
        isinstance(text, str)
        and text != ""
        and all(_LITERAL.fullmatch(part) for part in text.split("."))
    )


def split_path(text: str) -> tuple[str, ...]:
    if not is_path(text):
        raise TaskError(f"{text!r} is not a dotted manifest path", code="tasks.path.invalid")
    return tuple(text.split("."))


def parse_pattern(text: str) -> Pattern:
    if not isinstance(text, str) or text == "":
        raise TaskError(f"{text!r} is not a manifest path pattern", code="tasks.path.invalid")
    segments = []
    for part in text.split("."):
        if _LITERAL.fullmatch(part):
            segments.append(Segment(part))
        elif _GLOB.fullmatch(part):
            segments.append(Segment(part[:-1], glob=True))
        else:
            raise TaskError(
                f"{text!r} is not a manifest path pattern: bad segment {part!r}",
                code="tasks.path.invalid",
            )
    return tuple(segments)


def overlaps(first: Pattern, second: Pattern) -> bool:
    """True when one names the other, a part of it, or something containing it."""
    return all(a.meets(b) for a, b in zip(first, second, strict=False))


def covers(pattern: Pattern, path: Sequence[str]) -> bool:
    """True when the path is the pattern's key or lies inside its subtree."""
    return len(pattern) <= len(path) and all(
        segment.matches(part) for segment, part in zip(pattern, path, strict=False)
    )


def _reaches_below(pattern: Pattern, path: Sequence[str]) -> bool:
    """True when the pattern names something strictly inside the path's subtree."""
    return len(pattern) > len(path) and all(
        segment.matches(part) for segment, part in zip(pattern, path, strict=False)
    )


def _lookup(plain: dict[str, Any], path: Sequence[str]) -> Any:
    node: Any = plain
    for part in path:
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return None
    return node


def pinned_paths(plain: dict[str, Any], reads: Iterable[str], owns: Iterable[str]) -> list[str]:
    """The paths a task reads, with the subtrees it owns cut out.

    A task's own result must never invalidate its own inputs, so wherever an
    owned key lies inside a read path, the read path is replaced by its children
    that are not owned. A read path with no value yet contributes nothing in that
    case: after the task's own keys are created under it, it still contributes
    nothing, so the pin is stable across the task's own merge.
    """
    owned = [parse_pattern(text) for text in owns]
    found: set[str] = set()

    def visit(path: tuple[str, ...], node: Any) -> None:
        if any(covers(pattern, path) for pattern in owned):
            return
        if not any(_reaches_below(pattern, path) for pattern in owned):
            found.add(".".join(path))
            return
        if node is None:
            return
        if not isinstance(node, dict):
            found.add(".".join(path))
            return
        for key, child in node.items():
            if not _LITERAL.fullmatch(str(key)):
                raise TaskError(
                    f"manifest key {key!r} under {'.'.join(path)} cannot be addressed by a path",
                    code="tasks.path.unaddressable",
                    detail={"parent": ".".join(path), "key": str(key)},
                )
            visit((*path, str(key)), child)

    for text in reads:
        path = split_path(text)
        visit(path, _lookup(plain, path))
    return sorted(found)
