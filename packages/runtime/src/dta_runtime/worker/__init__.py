"""The twin's worker: runs the twin's models and the code a twin brings, as the API asks.

`serve(settings, host=..., port=...)` runs the service; `create_app(settings)` makes
its application. Both are loaded on first use: the processes that run code start
from this package too, and must not pay for the web framework.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .app import create_app, serve

__all__ = ["create_app", "serve"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from . import app

        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
