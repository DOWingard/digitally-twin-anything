"""The workspace's `.env`: which variables it sets. Their values are never shown.

The file is the one place where a machine's ports, secrets and source
locations live. The toolkit reads it only to tell which names are set and to
keep their values out of anything it prints.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from dotenv import dotenv_values
from dta_core.errors import DtaError
from dta_core.log import Log

log = Log("envfile")

MASK = "***"
# Shorter values are too likely to occur by chance in ordinary text.
_MIN_MASKED_LENGTH = 6


class EnvFileError(DtaError):
    """The environment file exists and cannot be read."""

    code = "env.unreadable"


def read_env(path: Path) -> dict[str, str]:
    """Variables an environment file sets, as written. A missing file sets none."""
    if not path.exists():
        log.debug("environment file not present", path=str(path))
        return {}
    try:
        values = dotenv_values(path, interpolate=False, encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.error("environment file cannot be read", exc=exc, path=str(path))
        raise EnvFileError(
            f"{path.name} cannot be read: {type(exc).__name__}", detail={"path": str(path)}
        ) from exc
    found = {name: value or "" for name, value in values.items()}
    log.debug("environment file read", path=str(path), variables=sorted(found))
    return found


def missing(required: Iterable[str], values: Mapping[str, str]) -> list[str]:
    """Required names that are absent or empty, in the order they were required."""
    return [name for name in dict.fromkeys(required) if not values.get(name)]


def scrub(text: str, values: Mapping[str, str]) -> str:
    """`text` with every value of the environment file replaced by a mask."""
    for value in sorted(set(values.values()), key=len, reverse=True):
        if len(value) >= _MIN_MASKED_LENGTH:
            text = text.replace(value, MASK)
    return text
