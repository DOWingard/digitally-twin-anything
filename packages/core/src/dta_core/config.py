"""Process configuration, read strictly from environment variables.

A required variable that is missing or empty stops the process at startup with
every missing name listed. Values are never logged.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

from .errors import ConfigError
from .log import Log

log = Log("config")


def require_env(names: Iterable[str], environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the values of `names`, or raise naming every one that is absent."""
    source = os.environ if environ is None else environ
    wanted = list(names)
    missing = [name for name in wanted if not source.get(name)]
    if missing:
        log.error("required environment variables are missing", missing=missing)
        raise ConfigError(
            f"missing environment variables: {', '.join(missing)}",
            code="config.missing_env",
            detail={"missing": missing},
        )
    log.debug("environment variables resolved", names=wanted)
    return {name: source[name] for name in wanted}


def env_int(name: str, environ: Mapping[str, str] | None = None) -> int:
    """A required integer variable."""
    raw = require_env([name], environ)[name]
    try:
        return int(raw)
    except ValueError as exc:
        log.error("environment variable is not an integer", exc=exc, name=name)
        raise ConfigError(
            f"{name} must be an integer", code="config.bad_value", detail={"name": name}
        ) from exc
