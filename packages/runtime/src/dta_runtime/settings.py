"""Settings of the twin's services, read strictly from the process environment.

A service stops at startup, naming every missing variable, rather than run
with a default it was not given.
"""

from __future__ import annotations

import os
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
ENV_SCHEDULER_POLL_S = "DTA_SCHEDULER_POLL_S"
ENV_WORKER_URL = "DTA_WORKER_URL"
ENV_WORKER_CPUS = "DTA_WORKER_CPUS"
ENV_WORKER_MEMORY_MB = "DTA_WORKER_MEMORY_MB"
MIN_WORKER_MEMORY_MB = 128

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


def _url(value: str, name: str) -> str:
    url = value.rstrip("/")
    if not url.startswith(("http://", "https://")):
        log.error("an address setting is not an http(s) URL", name=name)
        raise ConfigError(
            f"{name} must be an http or https URL", code="config.bad_value", detail={"name": name}
        )
    return url


@dataclass(frozen=True)
class RuntimeSettings:
    """What the API and the sync pipeline need."""

    store: StoreSettings
    api_token: str = field(repr=False)
    # Files kept outside the graph: rejected rows, exports, model artifacts.
    artifacts_dir: Path
    logs_dir: Path
    # Where the worker is. The stack sets it only for a twin that has a worker (one with
    # a model or with code of its own); None means the twin has neither.
    worker_url: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RuntimeSettings:
        values = require_env((ENV_API_TOKEN, ENV_ARTIFACTS_DIR, ENV_LOGS_DIR), environ)
        given = (os.environ if environ is None else environ).get(ENV_WORKER_URL)
        worker_url = _url(given, ENV_WORKER_URL) if given is not None else None
        log.info("worker of the twin", configured=worker_url is not None)
        return cls(
            store=StoreSettings.from_env(environ),
            api_token=_token(values),
            artifacts_dir=_directory(values, ENV_ARTIFACTS_DIR),
            logs_dir=_directory(values, ENV_LOGS_DIR),
            worker_url=worker_url,
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
        url = _url(values[ENV_API_URL], ENV_API_URL)
        return cls(api_url=url, api_token=_token(values), logs_dir=_directory(values, ENV_LOGS_DIR))


@dataclass(frozen=True)
class SchedulerSettings:
    """What the scheduler needs: where the API is, the token, and how often to look."""

    api_url: str
    api_token: str = field(repr=False)
    logs_dir: Path
    # Seconds on the wall between two looks at the twin's clock. A twin whose
    # clock runs faster than the wall needs a shorter one than its sync interval.
    poll_s: float

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> SchedulerSettings:
        names = (ENV_API_URL, ENV_API_TOKEN, ENV_LOGS_DIR, ENV_SCHEDULER_POLL_S)
        values = require_env(names, environ)
        url = _url(values[ENV_API_URL], ENV_API_URL)
        try:
            poll_s = float(values[ENV_SCHEDULER_POLL_S])
        except ValueError:
            poll_s = 0.0
        if not 0.05 <= poll_s <= 3600:
            log.error("the scheduler's poll period is out of range", name=ENV_SCHEDULER_POLL_S)
            raise ConfigError(
                f"{ENV_SCHEDULER_POLL_S} must be a number of seconds between 0.05 and 3600",
                code="config.bad_value",
                detail={"name": ENV_SCHEDULER_POLL_S},
            )
        return cls(
            api_url=url,
            api_token=_token(values),
            logs_dir=_directory(values, ENV_LOGS_DIR),
            poll_s=poll_s,
        )


@dataclass(frozen=True)
class WorkerSettings:
    """What the worker needs: where the API is, the token, and the limits its runs share.

    It holds nothing else: no store password, no source credential, no directory shared
    with another service.
    """

    api_url: str
    api_token: str = field(repr=False)
    # CPUs and memory the stack gives the worker's container; its pools and the limits
    # of each run are sized from them.
    cpus: float
    memory_mb: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> WorkerSettings:
        names = (ENV_API_URL, ENV_API_TOKEN, ENV_WORKER_CPUS, ENV_WORKER_MEMORY_MB)
        values = require_env(names, environ)
        try:
            cpus = float(values[ENV_WORKER_CPUS])
        except ValueError:
            cpus = 0.0
        if not 0 < cpus <= 1024:
            log.error("the worker's CPU limit is out of range", name=ENV_WORKER_CPUS)
            raise ConfigError(
                f"{ENV_WORKER_CPUS} must be a number of CPUs above 0",
                code="config.bad_value",
                detail={"name": ENV_WORKER_CPUS},
            )
        try:
            memory_mb = int(values[ENV_WORKER_MEMORY_MB])
        except ValueError:
            memory_mb = 0
        if memory_mb < MIN_WORKER_MEMORY_MB:
            log.error("the worker's memory limit is out of range", name=ENV_WORKER_MEMORY_MB)
            raise ConfigError(
                f"{ENV_WORKER_MEMORY_MB} must be a whole number of megabytes, at least "
                f"{MIN_WORKER_MEMORY_MB}",
                code="config.bad_value",
                detail={"name": ENV_WORKER_MEMORY_MB},
            )
        return cls(
            api_url=_url(values[ENV_API_URL], ENV_API_URL),
            api_token=_token(values),
            cpus=cpus,
            memory_mb=memory_mb,
        )
