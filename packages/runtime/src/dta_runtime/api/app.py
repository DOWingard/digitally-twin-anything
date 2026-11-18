"""The twin's HTTP API: the one way the browser interface, agents and pipelines read a twin."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

import anyio.to_thread
from dta_core.errors import ConfigError, DtaError
from dta_core.log import Log
from dta_core.store import StoreUnavailable
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response

from ..actions import HttpExecutor
from ..clock import Clock
from ..graph import GraphCache
from ..notify import BACKOFF_S, LOG, Notifier, notifier_for
from ..settings import ENV_API_TOKEN, MIN_TOKEN_LENGTH, RuntimeSettings
from ..sync.extract import Connect
from ..twin import Twin, open_twin
from .docs import API_PREFIX, OPEN_PATHS, build_openapi
from .fidelity_work import CORRECTION_BUDGET_S
from .jobs import JobRunner
from .live import Clocks
from .middleware import ApiMiddleware
from .models import Health
from .problems import error_response, problem
from .routes import (
    actions,
    artifacts,
    block,
    control,
    derived,
    fidelity,
    functions,
    graph,
    history,
    lifecycle,
    meta,
    models,
    objects,
    questions,
    scenarios,
    search,
    sync,
)
from .scenario_runs import sweep_interrupted
from .services import Services

log = Log("api")

__all__ = ["API_PREFIX", "GRAPH_CACHE_VERSIONS", "create_app"]

GRAPH_CACHE_VERSIONS = 4
_HTTP_CODES = {404: "api.not_found", 405: "api.method_not_allowed"}


def _version() -> str:
    try:
        return package_version("dta-runtime")
    except PackageNotFoundError:
        return "0"


async def _on_dta_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, DtaError)
    return error_response(exc)


async def _on_invalid_request(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    problems = [
        {"loc": [str(part) for part in error.get("loc", ())], "msg": error.get("msg", "")}
        for error in exc.errors()
    ]
    log.info("request refused: invalid parameters", count=len(problems))
    first = problems[0] if problems else {"loc": [], "msg": "invalid request"}
    detail = f"{'.'.join(first['loc'])}: {first['msg']}" if first["loc"] else first["msg"]
    return problem(400, "api.bad_request", f"invalid request: {detail}", errors=problems)


async def _on_http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    code = _HTTP_CODES.get(exc.status_code, "api.bad_request")
    status = exc.status_code if exc.status_code in _HTTP_CODES else 400
    return problem(status, code, str(exc.detail), headers=getattr(exc, "headers", None))


def create_app(
    settings: RuntimeSettings,
    twin: Twin | None = None,
    *,
    clock: Clock | None = None,
    connect: Connect | None = None,
    graph_cache_versions: int = GRAPH_CACHE_VERSIONS,
    notify_backoff_s: Sequence[float] = BACKOFF_S,
    correction_budget_s: float = CORRECTION_BUDGET_S,
) -> FastAPI:
    """The API of the twin whose store `settings` names.

    The twin is opened when the application starts and closed when it stops.
    A twin passed in is used as it is and left open. So is a clock: by default
    the twin's clock is the one its manifest names. `connect` opens the
    sources of a sync run; by default the real connectors do.

    Notifications go where the manifest in force at start says; a webhook whose
    variable is not set stops the start, naming it. So does a variable an action's
    executor names, when the executors add-on is on. `notify_backoff_s` are the
    waits between the tries of one delivery, `correction_budget_s` the time one
    correction of the fidelity ladder may take.
    """
    if not settings.api_token or len(settings.api_token) < MIN_TOKEN_LENGTH:
        log.error("the API token is missing or too short", name=ENV_API_TOKEN)
        raise ConfigError(
            f"{ENV_API_TOKEN} must hold at least {MIN_TOKEN_LENGTH} characters",
            code="config.bad_value",
            detail={"name": ENV_API_TOKEN},
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        started = time.perf_counter()
        try:
            opened = (
                twin
                if twin is not None
                else await anyio.to_thread.run_sync(open_twin, settings.store)
            )
        except DtaError as exc:
            log.error(
                "the API cannot start: the twin cannot be opened",
                exc=exc,
                code=exc.code,
                database=settings.store.database,
            )
            raise
        try:
            manifest = await anyio.to_thread.run_sync(
                lambda: opened.manifest(at=opened.store.head())
            )
        except StoreUnavailable as exc:
            # A twin handed in may have its store down: the API still starts and answers
            # 503 to what needs the store, as it always did. Its manifest cannot be read,
            # so notifications are logged only, and no action makes an external call, until
            # the API is started again.
            log.error(
                "the manifest cannot be read at start: notifications are only logged and "
                "actions are not executed until the API starts again",
                exc=exc,
                code=exc.code,
            )
            manifest = None
        notifier = Notifier(twin_id="twin", kind=LOG)
        executor: HttpExecutor | None = None
        if manifest is not None:
            try:
                notifier = notifier_for(manifest, os.environ, backoff_s=notify_backoff_s)
            except DtaError as exc:
                log.error(
                    "the API cannot start: its notifications cannot be set up",
                    exc=exc,
                    code=exc.code,
                    detail=exc.detail,
                )
                if twin is None:
                    opened.close()
                raise
            try:
                # Built once: a variable an executor names and the environment lacks stops
                # the start here, named, rather than failing the first action later.
                executor = HttpExecutor.from_manifest(manifest, os.environ)
            except DtaError as exc:
                log.error(
                    "the API cannot start: the executors of its actions cannot be built",
                    exc=exc,
                    code=exc.code,
                    detail=exc.detail,
                )
                if twin is None:
                    opened.close()
                raise
        # Handlers talking to the store run in worker threads. With no more
        # workers than pooled connections, a burst of requests waits for a
        # worker instead of failing for want of a connection.
        workers = settings.store.pool_size + settings.store.pool_overflow
        anyio.to_thread.current_default_thread_limiter().total_tokens = workers
        jobs = JobRunner()
        jobs.start()
        notifier.start()
        services = Services(
            twin=opened,
            settings=settings,
            graphs=GraphCache(opened.store, capacity=graph_cache_versions),
            jobs=jobs,
            clocks=Clocks(clock, os.environ),
            connect=connect,
            notifier=notifier,
            executor=executor,
            correction_budget_s=correction_budget_s,
        )
        app.state.services = services
        try:
            # No job runs yet, so a scenario still marked as running was left so by a
            # process that ended in the middle of it.
            swept = await anyio.to_thread.run_sync(sweep_interrupted, services)
        except DtaError as exc:
            # The API still serves what can be read; a run of such a scenario sweeps again.
            log.error("scenarios left running could not be looked for", exc=exc, code=exc.code)
            swept = []
        log.info(
            "API started",
            database=settings.store.database,
            workers=workers,
            graph_cache_versions=graph_cache_versions,
            logs_dir=str(settings.logs_dir),
            clock_given=clock is not None,
            worker=services.compute.configured,
            notifications=notifier.kind,
            executor=executor is not None,
            scenarios_interrupted=len(swept),
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        try:
            yield
        finally:
            # The job in progress may be writing to the twin: it ends before the store closes.
            await anyio.to_thread.run_sync(jobs.shutdown)
            await anyio.to_thread.run_sync(services.close)
            services.clocks.close()
            graphs = services.graphs
            if twin is None:
                opened.close()
            log.info(
                "API stopped",
                database=settings.store.database,
                graph_builds=graphs.builds,
                graph_hits=graphs.hits,
            )

    app = FastAPI(
        title="Digital twin API",
        version=_version(),
        description="Access to a digital twin: a versioned, typed graph of a real system. "
        "Every route but /healthz needs `Authorization: Bearer <token>`. Data is read at a "
        "version chosen with `at` and `branch`; each response says which version it describes. "
        "A live twin is kept in step with its sources by sync runs, started as background "
        "jobs. Errors are RFC 9457 problem documents with a stable `code`.",
        lifespan=lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.add_exception_handler(DtaError, _on_dta_error)
    app.add_exception_handler(RequestValidationError, _on_invalid_request)
    app.add_exception_handler(StarletteHTTPException, _on_http_error)

    for module in (
        meta,
        history,
        objects,
        search,
        graph,
        block,
        lifecycle,
        sync,
        artifacts,
        functions,
        models,
        questions,
        scenarios,
        derived,
        fidelity,
        actions,
        control,
    ):
        app.include_router(module.router)

    @app.get(f"{API_PREFIX}/openapi.json", include_in_schema=False)
    async def openapi_document() -> Response:
        return JSONResponse(build_openapi(app))

    @app.get(
        "/healthz",
        summary="Liveness of the API process",
        description='Needs no token and reads nothing; answers `{"status": "ok"}`.',
        response_model=Health,
        tags=["health"],
    )
    async def healthz() -> Response:
        return JSONResponse({"status": "ok"})

    app.openapi = lambda: build_openapi(app)  # type: ignore[method-assign]
    app.add_middleware(ApiMiddleware, token=settings.api_token, open_paths=OPEN_PATHS)
    return app
