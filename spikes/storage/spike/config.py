"""Runtime configuration, read strictly from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .log import Log

log = Log("config")

_REQUIRED = (
    "SPIKE_DOLT_ROOT_PASSWORD",
    "SPIKE_DOLT_PORT",
    "SPIKE_TERMINUS_ADMIN_PASS",
    "SPIKE_TERMINUS_PORT",
)


class ConfigError(RuntimeError):
    """Raised at startup when the environment is incomplete or malformed."""


@dataclass(frozen=True)
class Config:
    host: str
    dolt_port: int
    dolt_password: str
    terminus_port: int
    terminus_password: str


def load_config() -> Config:
    """Fail fast if any required variable is absent; never log secret values."""
    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        log.error("required environment variables are missing", missing=missing)
        raise ConfigError(f"missing environment variables: {', '.join(missing)}")
    try:
        dolt_port = int(os.environ["SPIKE_DOLT_PORT"])
        terminus_port = int(os.environ["SPIKE_TERMINUS_PORT"])
    except ValueError as exc:
        log.error("port variables must be integers", exc=exc)
        raise ConfigError("SPIKE_DOLT_PORT and SPIKE_TERMINUS_PORT must be integers") from exc
    config = Config(
        host="127.0.0.1",
        dolt_port=dolt_port,
        dolt_password=os.environ["SPIKE_DOLT_ROOT_PASSWORD"],
        terminus_port=terminus_port,
        terminus_password=os.environ["SPIKE_TERMINUS_ADMIN_PASS"],
    )
    log.info(
        "configuration loaded",
        host=config.host,
        dolt_port=config.dolt_port,
        terminus_port=config.terminus_port,
    )
    return config
