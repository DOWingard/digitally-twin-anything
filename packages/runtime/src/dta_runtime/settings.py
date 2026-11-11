"""Settings of the twin's services, read strictly from the process environment.

A service stops at startup, naming every missing variable, rather than run
with a default it was not given.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dta_core.config import require_env
from dta_core.errors import ConfigError
from dta_core.log import Log
from dta_core.store import StoreSettings

log = Log("runtime.settings")

ENV_API_TOKEN = "DTA_API_TOKEN"
ENV_ARTIFACTS_DIR = "DTA_ARTIFACTS_DIR"
ENV_LOGS_DIR = "DTA_LOGS_DIR"
ENV_API_URL = "DTA_API_URL"

# A shorter token is refused: it is the only thing between a caller and the twin.
MIN_TOKEN_LENGTH = 24

# Every service writes its log lines to `<logs dir>/<service>.jsonl` as well as
# to standard error, so that the API can serve the logs of the whole stack.
LOG_FILE_SUFFIX = ".jsonl"


def log_file(logs_dir: Path, service: str) -> Path:
    return logs_dir / f"{service}{LOG_FILE_SUFFIX}"


def _token(values: Mapping[str, str]) -> str:
    token = values[ENV_API_TOKEN]
    if len(token) < MIN_TOKEN_LENGTH:
        log.error("the API token is too short", name=ENV_API_TOKEN, minimum=MIN_TOKEN_LENGTH)
        raise ConfigError(
            f"{ENV_API_TOKEN} must hold at least {MIN_TOKEN_LENGTH} characters",
            code="config.bad_value",
            detail={"name": ENV_API_TOKEN, "minimum_length": MIN_TOKEN_LENGTH},
        )
    return token


def _directory(values: Mapping[str, str], name: str) -> Path:
    path = Path(values[name])
    if not path.is_absolute():
        log.error("a directory setting is not an absolute path", name=name)
        raise ConfigError(
            f"{name} must be an absolute path", code="config.bad_value", detail={"name": name}
        )
    return path


@dataclass(frozen=True)
class RuntimeSettings:
    """What the API and the sync pipeline need."""

    store: StoreSettings
    api_token: str = field(repr=False)
    # Files kept outside the graph: rejected rows, exports, model artifacts.
    artifacts_dir: Path
    logs_dir: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RuntimeSettings:
        values = require_env((ENV_API_TOKEN, ENV_ARTIFACTS_DIR, ENV_LOGS_DIR), environ)
        return cls(
            store=StoreSettings.from_env(environ),
            api_token=_token(values),
            artifacts_dir=_directory(values, ENV_ARTIFACTS_DIR),
            logs_dir=_directory(values, ENV_LOGS_DIR),
        )


@dataclass(frozen=True)
class UiSettings:
    """What the browser interface needs. It reaches the twin only through the API."""

    api_url: str
    api_token: str = field(repr=False)
    logs_dir: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> UiSettings:
        values = require_env((ENV_API_URL, ENV_API_TOKEN, ENV_LOGS_DIR), environ)
        url = values[ENV_API_URL].rstrip("/")
        if not url.startswith(("http://", "https://")):
            log.error("the API address is not an http(s) URL", name=ENV_API_URL)
            raise ConfigError(
                f"{ENV_API_URL} must be an http or https URL",
                code="config.bad_value",
                detail={"name": ENV_API_URL},
            )
        return cls(api_url=url, api_token=_token(values), logs_dir=_directory(values, ENV_LOGS_DIR))
