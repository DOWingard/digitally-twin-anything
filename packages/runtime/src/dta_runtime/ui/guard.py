"""Which requests the interface answers.

The interface holds the API token and acts for whoever reaches it. Its port
is published on the loopback address only, but that alone does not keep
another site out: a page whose name is made to resolve to this machine
reaches the interface from the operator's own browser, as if it were the
interface's page. Such a request names the other site as its host, so the
interface answers only under the loopback names. A request that changes
something must also come from a page the interface served: its origin, when
the browser states one, has to be the address the request was sent to.

Both refusals are 403 with a problem document. `ui.host_refused`: the host
is not a loopback name. `ui.origin_refused`: the request came from a page of
another origin.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from dta_core.log import Log
from flask import Flask, Response, request
from werkzeug.exceptions import SecurityError

log = Log("ui.guard")

# Names the interface is reached under. An IPv6 literal is not among them:
# the stack publishes the port on 127.0.0.1 only.
SERVED_HOSTS = ("127.0.0.1", "localhost")
HOST_REFUSED = "ui.host_refused"
ORIGIN_REFUSED = "ui.origin_refused"
PROBLEM_MEDIA = "application/problem+json"

_READS = frozenset({"GET", "HEAD", "OPTIONS"})
# Longest piece of a refused header written to the log: it is whatever the caller sent.
_LOGGED = 200


def _problem(code: str, title: str, detail: str) -> Response:
    document = {
        "type": f"urn:dta:error:{code}",
        "title": title,
        "status": 403,
        "detail": detail,
        "code": code,
    }
    return Response(json.dumps(document, separators=(",", ":")), status=403, mimetype=PROBLEM_MEDIA)


def same_origin(origin: str, host: str) -> bool:
    """Whether a page of `origin` is one served at `host` (the request's Host header)."""
    try:
        stated = urlsplit(origin)
    except ValueError:
        return False
    return stated.scheme in ("http", "https") and stated.netloc.lower() == host.lower()


def install(server: Flask) -> None:
    """Make `server` refuse requests for another host and changes asked by another origin."""
    # Flask checks the host of every request against this list while routing it.
    server.config["TRUSTED_HOSTS"] = list(SERVED_HOSTS)

    @server.errorhandler(SecurityError)
    def host_refused(error: SecurityError) -> Response:
        log.warn(
            "request refused: not addressed to the interface",
            code=HOST_REFUSED,
            host=request.headers.get("Host", "")[:_LOGGED],
            method=request.method,
            path=request.path[:_LOGGED],
        )
        return _problem(
            HOST_REFUSED,
            "Host refused",
            "The interface answers only under " + " and ".join(SERVED_HOSTS) + ".",
        )

    @server.before_request
    def origin_checked() -> Response | None:
        if request.method in _READS:
            return None
        origin = request.headers.get("Origin")
        # A request without the header was not made by a page of another site:
        # browsers state the origin of every cross-site request that is not a read.
        if origin is None or same_origin(origin, request.host):
            return None
        log.warn(
            "request refused: asked by a page of another origin",
            code=ORIGIN_REFUSED,
            origin=origin[:_LOGGED],
            method=request.method,
            path=request.path[:_LOGGED],
        )
        return _problem(
            ORIGIN_REFUSED,
            "Origin refused",
            "The interface accepts a change only from its own pages.",
        )

    log.info("requests guarded", hosts=list(SERVED_HOSTS))


__all__ = ["HOST_REFUSED", "ORIGIN_REFUSED", "SERVED_HOSTS", "install", "same_origin"]
