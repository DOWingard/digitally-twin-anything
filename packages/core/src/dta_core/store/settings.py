"""Connection settings of the store, read from the process environment."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..config import env_int, require_env
from .errors import invalid

ENV_HOST = "DTA_STORE_HOST"
ENV_PORT = "DTA_STORE_PORT"
ENV_USER = "DTA_STORE_USER"
ENV_PASSWORD = "DTA_STORE_PASSWORD"
ENV_DATABASE = "DTA_STORE_DATABASE"
ENV_NAMES = (ENV_HOST, ENV_PORT, ENV_USER, ENV_PASSWORD, ENV_DATABASE)

# The name is placed in SQL between backquotes, so it is held to plain characters.
DATABASE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


@dataclass(frozen=True)
class StoreSettings:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str
    pool_size: int = 8
    pool_overflow: int = 8
    # Longest wait for a free pooled connection.
    pool_timeout_s: float = 30.0
    connect_timeout_s: float = 10.0
    query_timeout_s: float = 600.0
    # Longest wait for another committer to finish.
    lock_timeout_s: float = 30.0
    # Operations slower than this are logged as warnings.
    slow_ms: float = 1000.0

    def __post_init__(self) -> None:
        if not DATABASE_NAME.match(self.database):
            raise invalid(
                "invalid_settings",
                "database name must start with a letter and hold only letters, digits and "
                f"underscores (at most 64): {self.database!r}",
            )
        if not 0 < self.port < 65536:
            raise invalid("invalid_settings", f"port out of range: {self.port}")
        if self.pool_size < 1 or self.pool_overflow < 0:
            raise invalid("invalid_settings", "pool_size must be at least 1")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> StoreSettings:
        """Read the five `DTA_STORE_*` variables; raises naming every one that is missing."""
        values = require_env(ENV_NAMES, environ)
        return cls(
            host=values[ENV_HOST],
            port=env_int(ENV_PORT, environ),
            user=values[ENV_USER],
            password=values[ENV_PASSWORD],
            database=values[ENV_DATABASE],
        )
