"""The edge of the store: nothing leaves it other than as a `StoreError`."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

import pymysql
import sqlalchemy.exc

from ..log import Log
from .db import error_code, error_text
from .errors import StoreError

log = Log("store")


def public[F: Callable[..., Any]](method: F) -> F:
    """Wrap a public method so that an error raised by the database driver or
    the connection pool reaches the caller as `store.database`, logged once."""

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        except StoreError:
            raise
        except (pymysql.MySQLError, sqlalchemy.exc.SQLAlchemyError) as exc:
            log.error("database error", exc=exc, operation=method.__name__)
            raise StoreError(
                f"{method.__name__} failed in the database: {error_text(exc)}",
                code="store.database",
                detail={"operation": method.__name__, "error_code": error_code(exc)},
            ) from exc

    return wrapper  # type: ignore[return-value]
