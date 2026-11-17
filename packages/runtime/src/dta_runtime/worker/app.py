"""The worker's HTTP service: `POST /run` and `GET /healthz`.

The listening process validates requests, admits them to a pool and answers; it never
imports a handler and never runs a twin's code. It holds the API's address and token
and nothing else.
"""

from __future__ import annotations

import hmac
import os
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dta_core.errors import ConfigError
from dta_core.log import Log, new_trace_id, trace
from fastapi import FastAPI, Request
from pydantic import TypeAdapter, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ..connections import SERVER_KEEP_ALIVE_S
from ..protocol import HANDLERS, FunctionRequest, WorkerRequest, runs_own_code
from ..settings import ENV_API_TOKEN, MIN_TOKEN_LENGTH, WorkerSettings
from .channel import OK
from .errors import (
    ARROW_MEDIA,
    BAD_REQUEST,
    FAILED,
    INTERNAL,
    JSON_MEDIA,
    LISTENER_STATUS,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    UNAUTHORIZED,
    WorkerError,
    problem,
    run_status,
)
from .pools import Pools, Ran

log = Log("worker")
request_log = Log("worker.request")

__all__ = ["create_app", "serve"]

REQUEST = TypeAdapter(WorkerRequest)
OPEN_PATHS = frozenset({"/healthz"})
RUN_INFO = "dta.worker.run"
# A request names what to read, never the data: it is small.
MAX_BODY_BYTES = 1024 * 1024
# Variables set by the worker itself, or holding the stack's own settings and token.
RESERVED_ENV = frozenset({"PATH", "LANG", "TMPDIR"})
RESERVED_PREFIX = "DTA_"
RESULT_MEDIA = frozenset({JSON_MEDIA, ARROW_MEDIA})


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


class Gate:
    """Outermost layer: the token, the one log line of every request, and the last
    defence against an unexpected error."""

    def __init__(self, app: ASGIApp, *, token: str) -> None:
        self.app = app
        self._token = token.encode("utf-8")

    def _authorized(self, scope: Scope) -> str:
        """`ok`, `missing` or `invalid`; only the log is told which."""
        value = dict(scope.get("headers") or []).get(b"authorization")
        if not value:
            return "missing"
        scheme, _, given = value.decode("latin-1").partition(" ")
        if scheme.lower() != "bearer" or not given.strip():
            return "invalid"
        return (
            "ok" if hmac.compare_digest(given.strip().encode("utf-8"), self._token) else "invalid"
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        info: dict[str, Any] = {
            "trace_id": new_trace_id(),
            "outcome": "ok",
            "status": 500,
            "started": False,
        }
        scope[RUN_INFO] = info

        async def send_status(message: Any) -> None:
            if message["type"] == "http.response.start":
                info["status"] = message["status"]
                info["started"] = True
            await send(message)

        try:
            if scope["path"] not in OPEN_PATHS:
                credentials = self._authorized(scope)
                if credentials != "ok":
                    info["outcome"] = UNAUTHORIZED
                    with trace(info["trace_id"]):
                        log.warn("request refused: no valid bearer token", reason=credentials)
                        refusal = problem(
                            401,
                            UNAUTHORIZED,
                            "a valid bearer token is required",
                            headers={"WWW-Authenticate": "Bearer"},
                        )
                    await refusal(scope, receive, send_status)
                    return
            with trace(info["trace_id"]):
                await self.app(scope, receive, send_status)
        except Exception as exc:
            info["outcome"] = INTERNAL
            with trace(info["trace_id"]):
                if info["started"]:
                    log.error("request failed after its answer had begun", exc=exc)
                    raise
                log.error("request failed unexpectedly", exc=exc, path=scope["path"])
                failure = problem(500, INTERNAL, "the request failed in the worker")
            await failure(scope, receive, send_status)
        finally:
            _log_request(scope, info, started)


def _log_request(scope: Scope, info: dict[str, Any], started: float) -> None:
    fields = {
        "method": scope["method"],
        "path": scope["path"],
        "op": info.get("op"),
        "pool": info.get("pool"),
        "own_code": info.get("own_code"),
        "outcome": info["outcome"],
        "status": info["status"],
        "waited_ms": info.get("waited_ms"),
        "duration_ms": _elapsed_ms(started),
    }
    with trace(info["trace_id"]):
        if scope["path"] in OPEN_PATHS:
            request_log.debug("request answered", **fields)
        else:
            request_log.info("request answered", **fields)


async def _body(request: Request) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise WorkerError(f"the body holds more than {MAX_BODY_BYTES} bytes", code=BAD_REQUEST)
        chunks.append(chunk)
    return b"".join(chunks)


def _parse(body: bytes) -> Any:
    try:
        return REQUEST.validate_json(body)
    except ValidationError as exc:
        # The location and kind of each problem, never the value that was sent.
        errors = [
            {"loc": [str(part) for part in error["loc"]], "type": error["type"]}
            for error in exc.errors(include_url=False, include_input=False, include_context=False)
        ]
        named = "; ".join(f"{'.'.join(e['loc']) or 'body'} ({e['type']})" for e in errors[:10])
        log.info("request refused: not a valid run", problems=len(errors))
        raise WorkerError(
            f"the body is not a valid run: {named}", code=BAD_REQUEST, extra={"errors": errors}
        ) from exc


def _check_env(request: Any) -> None:
    if not isinstance(request, FunctionRequest):
        return
    reserved = sorted(
        name for name in request.env if name in RESERVED_ENV or name.startswith(RESERVED_PREFIX)
    )
    if reserved:
        log.warn("request refused: it declares variables the worker keeps", names=reserved)
        raise WorkerError(
            f"variables the worker sets or keeps cannot be declared: {', '.join(reserved)}",
            code=BAD_REQUEST,
            extra={"names": reserved},
        )
    missing = sorted(name for name in request.env if name not in os.environ)
    if missing:
        log.warn("request refused: it declares variables the worker does not have", names=missing)
        raise WorkerError(
            f"the worker has no variable {', '.join(missing)}",
            code=BAD_REQUEST,
            extra={"names": missing},
        )


def _answer(ran: Ran, trace_id: str) -> tuple[Response, str]:
    reply = ran.reply
    if reply.kind == OK:
        if reply.media_type in RESULT_MEDIA:
            return Response(content=reply.body, media_type=reply.media_type), "ok"
        log.error("a run answered a result of no known kind", media_type=reply.media_type)
        return (
            problem(
                500,
                FAILED,
                "the run answered a result of no known kind",
                trace_id=trace_id,
                extra={"frames": []},
            ),
            FAILED,
        )
    code = reply.code or FAILED
    extra = {"frames": reply.frames} if code == FAILED else None
    return problem(run_status(code), code, reply.message, trace_id=trace_id, extra=extra), code


def create_app(
    settings: WorkerSettings,
    *,
    handlers: Mapping[str, str] = HANDLERS,
    handler_paths: Sequence[str | Path] = (),
    work_dir: Path | None = None,
) -> FastAPI:
    """The worker of the twin whose API `settings` names.

    `handlers` maps each op to the `module:name` of its handler; `handler_paths` are
    directories searched for handler modules after the installed packages (for
    handlers that are not part of the image, such as a test's). `work_dir` holds the
    unpacked code and the runs' temporary directories; by default a directory under
    the system's temporary directory.
    """
    if not settings.api_token or len(settings.api_token) < MIN_TOKEN_LENGTH:
        log.error("the API token is missing or too short", name=ENV_API_TOKEN)
        raise ConfigError(
            f"{ENV_API_TOKEN} must hold at least {MIN_TOKEN_LENGTH} characters",
            code="config.bad_value",
            detail={"name": ENV_API_TOKEN},
        )
    chosen = dict(handlers)
    unknown = sorted(set(HANDLERS) - set(chosen))
    if unknown:
        raise ConfigError(f"no handler is given for {', '.join(unknown)}", code="config.bad_value")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        started = time.perf_counter()
        pools = Pools(
            settings,
            handlers=chosen,
            handler_paths=[str(path) for path in handler_paths],
            work_dir=work_dir,
        )
        try:
            await pools.open()
        except Exception as exc:
            log.error("the worker cannot start: its pools cannot be opened", exc=exc)
            raise
        app.state.pools = pools
        log.info(
            "worker started",
            api_url=settings.api_url,
            cpus=settings.cpus,
            memory_mb=settings.memory_mb,
            elapsed_ms=_elapsed_ms(started),
        )
        try:
            yield
        finally:
            await pools.close()
            log.info("worker stopped")

    app = FastAPI(
        title="Digital twin worker",
        lifespan=lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )

    @app.exception_handler(StarletteHTTPException)
    async def _on_http_error(request: Request, exc: StarletteHTTPException) -> Response:
        code = METHOD_NOT_ALLOWED if exc.status_code == 405 else NOT_FOUND
        request.scope[RUN_INFO]["outcome"] = code
        return problem(LISTENER_STATUS[code], code, str(exc.detail))

    @app.get("/healthz")
    async def healthz(request: Request) -> Response:
        pools: Pools | None = getattr(request.app.state, "pools", None)
        if pools is None or not pools.started:
            return JSONResponse({"status": "starting"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.post("/run")
    async def run(request: Request) -> Response:
        info: dict[str, Any] = request.scope[RUN_INFO]
        try:
            parsed = _parse(await _body(request))
        except WorkerError as exc:
            info["outcome"] = exc.code
            with trace(info["trace_id"]):
                return problem(LISTENER_STATUS[exc.code], exc.code, exc.message, extra=exc.extra)
        info.update(
            trace_id=parsed.trace_id,
            op=parsed.op,
            own_code=runs_own_code(parsed),
            pool=request.app.state.pools.pool_for(parsed.op).name,
        )
        with trace(parsed.trace_id):
            log.debug(
                "run asked",
                op=parsed.op,
                budget_s=parsed.budget_s,
                args=sorted(getattr(parsed, "args", {})),
                params=sorted(getattr(parsed, "params", {})),
                env=list(getattr(parsed, "env", [])),
            )
            try:
                _check_env(parsed)
                ran = await request.app.state.pools.run(parsed)
            except WorkerError as exc:
                info["outcome"] = exc.code
                return problem(
                    LISTENER_STATUS.get(exc.code, run_status(exc.code)),
                    exc.code,
                    exc.message,
                    headers=exc.headers,
                    extra=exc.extra,
                )
            info["waited_ms"] = ran.waited_ms
            response, info["outcome"] = _answer(ran, parsed.trace_id)
            return response

    app.add_middleware(Gate, token=settings.api_token)
    return app


def serve(settings: WorkerSettings, *, host: str, port: int) -> None:
    """Serve the worker until it is told to stop (SIGTERM, SIGINT)."""
    import uvicorn

    log.info("worker serving", host=host, port=port, keep_alive_s=SERVER_KEEP_ALIVE_S)
    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_config=None,
        access_log=False,
        timeout_keep_alive=SERVER_KEEP_ALIVE_S,
    )
