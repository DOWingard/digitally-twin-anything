"""The call an action makes to the real system.

An action bound `executor` carries an HTTP call in its type: a method, and templates
of the address, the headers and the body. In a template, `${NAME}` is the value of an
environment variable the executor lists in its `env`; `{uid}` is the uid of the object
acted on and `{id}` that uid after its first colon; `{<param>}` is a parameter of the
proposal. A body value `{"from": "<param>", "map": {...}}` sends the mapped value of a
parameter, and a body text that is a single `{<param>}` sends the parameter with its
own type. The header `Idempotency-Key` is the record's id, so a call sent again for the
same record is the same call to the system that receives it.

Nothing filled into a template is logged or stored: the variables may hold secrets.
What is logged is the action, the record, the method, the status and the duration;
what is kept is the status, the duration and the start of the answer's body, with any
variable's value masked.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from dta_core.config import require_env
from dta_core.connect.base import Redactor
from dta_core.errors import ConfigError
from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.manifest.checks.view import walk_strings
from dta_core.sources import placeholders

from ..connections import client_limits
from .checks import same
from .errors import EXECUTOR_INVALID, refused
from .records import EXECUTED, EXECUTOR, FAILED, bare

log = Log("runtime.actions")

IDEMPOTENCY_KEY = "Idempotency-Key"
TIMEOUT_S = 30.0
# A connection that cannot be made is tried this many more times; nothing was sent, so
# trying again cannot act twice. An answer, even an error, is never asked for again.
RETRIES = 2
BACKOFF_S = 0.5
EXCERPT_CHARS = 2000
_TEMPLATE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Loggers of the HTTP library that may write an address it was given.
_LIBRARY_LOGGERS = (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)


@dataclass(frozen=True)
class Execution:
    """What came of a call: `executed` (the system answered 2xx) or `failed`.

    `effective_at` is the time the answer says the change takes effect, when it says
    one; `excerpt` the start of the answer's body; `error` why no answer came, or why
    the call could not be made, without anything filled into its templates.
    """

    status: str
    http_status: int | None = None
    duration_ms: float = 0.0
    excerpt: str | None = None
    effective_at: datetime | None = None
    attempts: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Executor(Protocol):
    """What makes an action's external call: the HTTP executor, or a test's own."""

    def __call__(self, action: Mapping[str, Any], record: Mapping[str, Any]) -> Execution:
        """Make the call of `action` (its type, as `types` gives it) for `record` (as
        `record` gives it). Never raises for what the external system does."""
        ...


class _Unrenderable(Exception):
    """A template names something that is not there; the message names it, never a value."""


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def _text(value: Any) -> str:
    """A parameter as it goes into a text: text as it is, anything else as JSON."""
    return value if isinstance(value, str) else json.dumps(value)


class _Request:
    """The templates of one action filled in for one record."""

    def __init__(
        self, values: Mapping[str, str], allowed: Iterable[str], record: Mapping[str, Any]
    ):
        self._values = values
        self._allowed = set(allowed)
        uid = str(record.get("object") or "")
        self._fixed = {"uid": uid, "id": uid.split(":", 1)[1] if ":" in uid else uid}
        self._params = dict(record.get("params") or {})

    def _variable(self, name: str) -> str:
        if name not in self._allowed:
            raise _Unrenderable(f"${{{name}}} is not among the variables the executor lists")
        if name not in self._values:
            raise _Unrenderable(f"${{{name}}} was not given to the executor when it was built")
        return self._values[name]

    def _named(self, name: str) -> Any:
        if name in self._fixed:
            return self._fixed[name]
        if name not in self._params:
            raise _Unrenderable(f"{{{name}}} is neither uid, id nor a parameter of the proposal")
        return self._params[name]

    def text(self, template: str, *, in_url: bool = False) -> str:
        def fill(match: re.Match[str]) -> str:
            if match.group(1) is not None:
                return self._variable(match.group(1))
            found = _text(self._named(match.group(2)))
            return quote(found, safe="") if in_url else found

        return _TEMPLATE.sub(fill, template)

    def _mapped(self, spec: Mapping[str, Any]) -> Any:
        name = spec.get("from")
        if not isinstance(name, str):
            raise _Unrenderable("a mapped body value names its parameter in `from`")
        value = self._named(name)
        if "map" not in spec:
            return value
        table = spec["map"]
        if not isinstance(table, Mapping):
            raise _Unrenderable(f"the map of {name} is not a mapping")
        for key, mapped in table.items():
            if same(key, value):
                return mapped
        for key, mapped in table.items():
            if isinstance(key, str) and key in (_text(value), str(value)):
                return mapped
        shown = _text(value)
        raise _Unrenderable(f"the map of {name} has no entry for {shown}")

    def body(self, template: Any) -> Any:
        if isinstance(template, Mapping):
            if "from" in template and set(template) <= {"from", "map"}:
                return self._mapped(template)
            return {key: self.body(item) for key, item in template.items()}
        if isinstance(template, list):
            return [self.body(item) for item in template]
        if isinstance(template, str):
            whole = _TEMPLATE.fullmatch(template)
            if whole is not None and whole.group(2) is not None:
                return self._named(whole.group(2))
            return self.text(template)
        return template


class _Masking(logging.Filter):
    """Masks the executor's variables in what the HTTP library logs, such as the address
    of each request it sends."""

    def __init__(self, redact: Redactor) -> None:
        super().__init__()
        self._redact = redact

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        masked = self._redact(message)
        if masked != message:
            record.msg, record.args = masked, None
        return True


def _effective_at(response: httpx.Response, action: str, record: str) -> datetime | None:
    try:
        document = response.json()
    except ValueError:
        log.debug("the answer is not JSON; it says no effective time", action=action, record=record)
        return None
    found = document.get("effective_at") if isinstance(document, Mapping) else None
    if found is None:
        return None
    try:
        moment = datetime.fromisoformat(found) if isinstance(found, str) else None
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None or moment.utcoffset() is None:
        log.warn(
            "the answer's effective_at is not a time with an offset; the time it was sent is "
            "taken instead",
            action=action,
            record=record,
        )
        return None
    return moment


class HttpExecutor:
    """Makes the HTTP calls of a twin's actions. Built once, from the manifest and the
    environment, and used from any thread; `close` it when it is no longer needed."""

    def __init__(
        self,
        values: Mapping[str, str],
        *,
        timeout_s: float = TIMEOUT_S,
        retries: int = RETRIES,
        backoff_s: float = BACKOFF_S,
    ) -> None:
        self._values = dict(values)
        self._timeout_s = timeout_s
        self._retries = retries
        self._backoff_s = backoff_s
        self._redact = Redactor(self._values.values())
        self._client = httpx.Client(
            limits=client_limits(), timeout=httpx.Timeout(timeout_s), follow_redirects=False
        )
        self._masking = _Masking(self._redact)
        for name in _LIBRARY_LOGGERS:
            logging.getLogger(name).addFilter(self._masking)

    @classmethod
    def from_manifest(
        cls,
        manifest: Manifest,
        environ: Mapping[str, str],
        *,
        timeout_s: float = TIMEOUT_S,
        retries: int = RETRIES,
        backoff_s: float = BACKOFF_S,
    ) -> HttpExecutor | None:
        """The executor of the manifest's actions; None when the executors add-on is off.

        Raises naming every variable an executor lists that the environment lacks, and
        every variable a template uses that its executor does not list.
        """
        contract = manifest.contract
        if contract is None or not contract.addons.executors:
            log.info("the executors add-on is off: no action makes an external call")
            return None
        names: dict[str, None] = {}
        bound = 0
        for action_id, action in sorted(manifest.actions.items()):
            executor = action.executor
            if action.binding != EXECUTOR or executor is None:
                continue
            bound += 1
            listed = set(executor.env)
            used = [*placeholders(executor.url or "")]
            for value in executor.headers.values():
                used.extend(placeholders(value))
            for _, text in walk_strings(executor.body, "body"):
                used.extend(placeholders(text))
            unlisted = sorted({name for name in used if name not in listed})
            if unlisted:
                log.error(
                    "an executor's templates use variables its env does not list",
                    action=action_id,
                    variables=unlisted,
                )
                raise refused(
                    EXECUTOR_INVALID,
                    f"actions.{action_id}.executor uses "
                    f"{', '.join('${' + name + '}' for name in unlisted)}, which its env does "
                    "not list",
                    action=action_id,
                    variables=unlisted,
                )
            names.update(dict.fromkeys(executor.env))
        try:
            values = require_env(names, environ)
        except ConfigError as exc:
            log.error(
                "the executors cannot be built: variables they need are not set",
                missing=exc.detail.get("missing"),
            )
            raise
        log.info(
            "executor built",
            actions=bound,
            variables=sorted(names),
            timeout_s=timeout_s,
            retries=retries,
        )
        return cls(values, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)

    def close(self) -> None:
        for name in _LIBRARY_LOGGERS:
            logging.getLogger(name).removeFilter(self._masking)
        self._client.close()

    def __enter__(self) -> HttpExecutor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _send(
        self, method: str, url: str, headers: dict[str, str], body: Any, context: dict[str, Any]
    ) -> tuple[httpx.Response | None, int, str | None]:
        """The answer, the number of attempts, and why there is no answer."""
        attempt = 0
        while True:
            attempt += 1
            try:
                options: dict[str, Any] = {"headers": headers}
                if body is not None:
                    options["json"] = body
                return self._client.request(method, url, **options), attempt, None
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # The exception's text may hold the address; only its class is told.
                if attempt > self._retries:
                    log.error(
                        "the external system cannot be reached; the action failed",
                        **context,
                        attempts=attempt,
                        failure=type(exc).__name__,
                    )
                    return (
                        None,
                        attempt,
                        f"no connection after {attempt} attempts ({type(exc).__name__})",
                    )
                wait = self._backoff_s * attempt
                log.warn(
                    "the external system cannot be reached; trying again",
                    **context,
                    attempt=attempt,
                    retries=self._retries,
                    backoff_s=wait,
                    failure=type(exc).__name__,
                )
                time.sleep(wait)
            except httpx.TimeoutException as exc:
                log.error(
                    "the external system did not answer in time; the action failed and is "
                    "not sent again",
                    **context,
                    attempts=attempt,
                    timeout_s=self._timeout_s,
                    failure=type(exc).__name__,
                )
                return (
                    None,
                    attempt,
                    f"no answer within {self._timeout_s:g} s ({type(exc).__name__})",
                )
            except httpx.HTTPError as exc:
                log.error(
                    "the call to the external system broke off; the action failed and is "
                    "not sent again",
                    **context,
                    attempts=attempt,
                    failure=type(exc).__name__,
                )
                return None, attempt, f"the call broke off ({type(exc).__name__})"

    def __call__(self, action: Mapping[str, Any], record: Mapping[str, Any]) -> Execution:
        started = time.perf_counter()
        spec = action.get("executor") or {}
        action_id = action.get("id") or bare(action.get("uid"))
        record_id = record.get("id") or bare(record.get("uid"))
        method = str(spec.get("method") or "POST").upper()
        context = {"action": action_id, "record": record.get("uid"), "method": method}
        if spec.get("kind") not in (None, "http") or not spec.get("url"):
            log.error("the action's executor is not an HTTP call with an address", **context)
            return Execution(
                FAILED,
                duration_ms=_elapsed_ms(started),
                error="the executor is not an HTTP call with an address",
            )
        filled = _Request(self._values, spec.get("env") or [], record)
        try:
            url = filled.text(str(spec["url"]), in_url=True)
            headers = {
                str(name): filled.text(str(value))
                for name, value in (spec.get("headers") or {}).items()
            }
            body = filled.body(spec["body"]) if spec.get("body") else None
        except _Unrenderable as exc:
            log.error(
                "the action's request cannot be made from its templates; nothing was sent",
                **context,
                problem=str(exc),
            )
            return Execution(
                FAILED,
                duration_ms=_elapsed_ms(started),
                error=f"the request cannot be made: {exc}",
            )
        headers[IDEMPOTENCY_KEY] = str(record_id)
        response, attempts, error = self._send(method, url, headers, body, context)
        duration_ms = _elapsed_ms(started)
        if response is None:
            return Execution(FAILED, duration_ms=duration_ms, attempts=attempts, error=error)
        excerpt = self._redact(response.text)[:EXCERPT_CHARS]
        if 200 <= response.status_code < 300:
            effective = _effective_at(response, str(action_id), str(record.get("uid")))
            log.info(
                "action sent",
                **context,
                status=response.status_code,
                attempts=attempts,
                duration_ms=duration_ms,
            )
            return Execution(
                EXECUTED,
                http_status=response.status_code,
                duration_ms=duration_ms,
                excerpt=excerpt,
                effective_at=effective,
                attempts=attempts,
            )
        log.error(
            "the external system refused the action; it is not sent again",
            **context,
            status=response.status_code,
            attempts=attempts,
            duration_ms=duration_ms,
        )
        return Execution(
            FAILED,
            http_status=response.status_code,
            duration_ms=duration_ms,
            excerpt=excerpt,
            attempts=attempts,
            error=f"the system answered {response.status_code}",
        )
