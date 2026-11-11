"""Read-only connector to JSON-over-HTTP interfaces, built on dlt's REST client.

`parse_options` checks a source's options; `open_http` returns a connector,
which is also a `TimeSource` when the options name a clock.
"""

from __future__ import annotations

from .connector import ClockedHttpConnector, HttpConnector, open_http
from .settings import HttpDataset, HttpOptions, parse_options

__all__ = [
    "ClockedHttpConnector",
    "HttpConnector",
    "HttpDataset",
    "HttpOptions",
    "open_http",
    "parse_options",
]
