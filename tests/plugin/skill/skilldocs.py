"""Reads the build skill the way its tests check it.

The skill is text an agent follows literally, so its tests read it as data: the
Markdown files and what they link to, the code spans and fenced blocks that hold
commands and field paths, and the tables whose first column names something the
tool must know (a field, a code, a gate, a check). The tool side comes from the
installed packages themselves: the command tree, the manifest schema and the
string literals of the sources.
"""

from __future__ import annotations

import re
import shlex
import types
import typing
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import typer
from dta_core.log import Log
from dta_core.manifest.models import Manifest
from dta_toolkit.cli import app
from pydantic import BaseModel
from typer._click.core import Context

log = Log("skill.docs")

ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT
SKILL = PLUGIN / "skills" / "build-twin"
SKILL_MD = SKILL / "SKILL.md"
REFERENCE = SKILL / "reference"
SOURCES = ROOT / "packages"

# A placeholder in a command is replaced by this token before the command is split.
PLACEHOLDER = "\x00placeholder"
_ANGLE = re.compile(r"<[^<>\n]+>")
_FENCE = re.compile(r"^```([A-Za-z0-9_-]*)\s*$")
_SPAN = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_LINK = re.compile(r"\[[^\]\n]*\]\(([^)\s]+)\)")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
# A dotted name such as a field path, an error code or a check id.
DOTTED = re.compile(r"^[a-z_]+(?:\.(?:[A-Za-z0-9_]+|<[^<>.\s]+>))+$")
_LITERAL = re.compile(r"[\"']([a-z_]+(?:\.[A-Za-z0-9_]+)+)[\"']")
_COMPOSED = re.compile(r"f[\"']([a-z_]+(?:\.[a-z_]+)*)\.\{")


@dataclass(frozen=True)
class Located:
    """A piece of a file with where it starts, for messages that point at it."""

    text: str
    path: Path
    line: int

    def where(self) -> str:
        return f"{self.path.relative_to(ROOT)}:{self.line}"


@dataclass(frozen=True)
class Table:
    header: list[str]
    rows: list[list[str]]
    path: Path
    line: int


def markdown_files() -> list[Path]:
    return sorted(SKILL.rglob("*.md"))


def skill_files() -> list[Path]:
    """Every file of the skill: what an agent may be sent to read."""
    return sorted(path for path in SKILL.rglob("*") if path.is_file())


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def frontmatter(path: Path) -> tuple[str, str]:
    """The YAML between the opening `---` lines, and the body after it."""
    text = read(path)
    if not text.startswith("---\n"):
        return "", text
    end = text.index("\n---\n", 4)
    return text[4:end], text[end + 5 :]


def _split_fences(path: Path) -> tuple[list[tuple[str, Located]], list[tuple[int, str]]]:
    """(fenced blocks with their language, prose lines with their numbers)."""
    blocks: list[tuple[str, Located]] = []
    prose: list[tuple[int, str]] = []
    lang: str | None = None
    body: list[str] = []
    start = 0
    for number, line in enumerate(read(path).splitlines(), start=1):
        fence = _FENCE.match(line.strip())
        if lang is None and fence:
            lang, body, start = fence.group(1), [], number + 1
        elif lang is not None and line.strip() == "```":
            blocks.append((lang, Located("\n".join(body), path, start)))
            lang = None
        elif lang is not None:
            body.append(line)
        else:
            prose.append((number, line))
    return blocks, prose


def fenced_blocks(path: Path) -> list[tuple[str, Located]]:
    return _split_fences(path)[0]


def spans(path: Path) -> list[Located]:
    """Inline code spans outside fenced blocks."""
    found = []
    for number, line in _split_fences(path)[1]:
        found += [Located(match.group(1), path, number) for match in _SPAN.finditer(line)]
    return found


def links(path: Path) -> list[Located]:
    found = []
    for number, line in _split_fences(path)[1]:
        found += [Located(match.group(1), path, number) for match in _LINK.finditer(line)]
    return found


def tables(path: Path) -> list[Table]:
    found: list[Table] = []
    current: list[tuple[int, str]] = []
    for number, line in [*_split_fences(path)[1], (0, "")]:
        if line.startswith("|"):
            current.append((number, line))
            continue
        if len(current) >= 2:
            cells = [_cells(text) for _, text in current]
            found.append(Table(cells[0], cells[2:], path, current[0][0]))
        current = []
    return found


def _cells(line: str) -> list[str]:
    parts = _CELL_SPLIT.split(line.strip())
    return [part.strip().replace("\\|", "|") for part in parts[1:-1]]


def tables_headed(path: Path, first: str) -> list[Table]:
    return [table for table in tables(path) if table.header and table.header[0] == first]


def unquote(cell: str) -> str:
    """A cell's text without the backticks around it."""
    return cell[1:-1] if cell.startswith("`") and cell.endswith("`") else cell


# -- commands -------------------------------------------------------------------------


def commands() -> list[Located]:
    """Every `dta` command the skill tells an agent to run: lines of fenced shell blocks
    and inline code spans that start with `dta`."""
    found: list[Located] = []
    for path in markdown_files():
        for lang, block in fenced_blocks(path):
            if lang in ("yaml", "json", "python", "markdown"):
                continue
            for offset, line in enumerate(block.text.splitlines()):
                text = line.strip().removeprefix("$ ")
                if text.startswith("dta "):
                    found.append(Located(text, path, block.line + offset))
        found += [span for span in spans(path) if span.text.startswith("dta ")]
    return found


@cache
def cli_root() -> Any:
    return typer.main.get_command(app)


@dataclass
class Parsed:
    """What walking one command line through the command tree found."""

    command: list[str]
    positionals: list[str]
    problems: list[str]
    generic: bool = False


def _options(command: Any) -> dict[str, Any]:
    found = {}
    for param in command.params:
        if getattr(param, "param_type_name", None) == "option":
            for name in [*param.opts, *param.secondary_opts]:
                found[name] = param
    return found


def parse_command(text: str) -> Parsed:
    """Walk `dta ...` through the real command tree: each word names a subcommand while
    there is a group, each option must belong to the command it follows."""
    try:
        tokens = shlex.split(_ANGLE.sub(PLACEHOLDER, text))
    except ValueError as exc:
        log.warn("a command of the skill cannot be split into words", text=text, error=str(exc))
        return Parsed([], [], [f"cannot be split into words: {exc}"])
    parsed = Parsed(["dta"], [], [])
    command = cli_root()
    context = Context(command, info_name="dta")
    index = 1
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token.startswith("-"):
            name = token.split("=", 1)[0]
            option = _options(command).get(name)
            if option is None:
                parsed.problems.append(f"{' '.join(parsed.command)} has no option {name}")
            elif "=" not in token and not option.is_flag:
                index += 1
            continue
        if hasattr(command, "list_commands"):
            if token == PLACEHOLDER:
                parsed.generic = True
                return parsed
            sub = command.get_command(context, token)
            if sub is None:
                parsed.problems.append(f"{' '.join(parsed.command)} has no command {token}")
                return parsed
            command, context = sub, Context(sub, info_name=token, parent=context)
            parsed.command.append(token)
            continue
        parsed.positionals.append(token)
    return parsed


# -- manifest field paths -------------------------------------------------------------


def _plain(annotation: Any) -> Any:
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union:
        rest = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        return rest[0] if len(rest) == 1 else Any
    return annotation


def resolves(path: str) -> bool:
    """Whether a dotted path names a field of the manifest schema. A segment in angle
    brackets, or any name, stands for a key where the schema has a map."""
    segments = path.split(".")
    if segments[0] == "field_status":
        return True
    node: Any = Manifest
    index = 0
    while index < len(segments):
        segment = segments[index]
        if isinstance(node, type) and issubclass(node, BaseModel):
            field = node.model_fields.get(segment)
            if field is None:
                return False
            node = _plain(field.annotation)
            index += 1
        elif typing.get_origin(node) is dict:
            node = _plain(typing.get_args(node)[1])
            index += 1
        elif node is Any:
            return True
        else:
            return False
    return True


def is_manifest_path(text: str) -> bool:
    """A dotted span whose first part is a top-level section of the manifest."""
    return bool(DOTTED.match(text)) and text.split(".")[0] in Manifest.model_fields


# -- the tool's sources ----------------------------------------------------------------


@cache
def source_texts() -> dict[Path, str]:
    return {path: read(path) for path in sorted(SOURCES.glob("*/src/**/*.py"))}


@cache
def literals() -> frozenset[str]:
    """Every dotted string literal in the tool's sources: codes, check ids, field paths."""
    found: set[str] = set()
    for text in source_texts().values():
        found.update(_LITERAL.findall(text))
    return frozenset(found)


def code_exists(code: str) -> bool:
    """Whether the sources can produce this code: as a literal, or composed by an f-string
    from a literal prefix and a last part that the same file quotes."""
    if code in literals():
        return True
    prefix, _, last = code.rpartition(".")
    for text in source_texts().values():
        if prefix in _COMPOSED.findall(text) and f'"{last}"' in text:
            return True
    return False
