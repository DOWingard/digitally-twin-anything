"""Entry point of the twin's own image: one image, one command per service.

Every command prints exactly one JSON document on standard output and logs to
standard error and to the stack's log directory. A command that cannot do its
work exits with a non-zero status and prints `{"error": {...}}`.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from dta_core import log as core_log
from dta_core.errors import DtaError
from dta_core.log import Log, new_trace_id, trace
from dta_core.store import StoreSettings

from .settings import RuntimeSettings, UiSettings, log_file

app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False)
log = Log("runtime.cli")

API_PORT = 8000
UI_PORT = 8050
WORKER_PORT = 8100
MCP_PORT = 8200
PORTS = {"api": API_PORT, "ui": UI_PORT, "worker": WORKER_PORT, "mcp": MCP_PORT}


def _print(document: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(document, default=str) + "\n")
    sys.stdout.flush()


def _run(service: str, work: Callable[[], dict[str, Any] | None]) -> None:
    """Run one command: logging set up, one trace id, errors as a JSON document."""
    logs_dir = os.environ.get("DTA_LOGS_DIR")
    try:
        core_log.configure(log_file=log_file(Path(logs_dir), service) if logs_dir else None)
    except OSError as exc:
        core_log.configure()
        log.warn("log file cannot be written; logging to standard error only", error=repr(exc))
    with trace(new_trace_id()) as trace_id:
        try:
            result = work()
        except DtaError as exc:
            log.error("command failed", exc=exc, command=service, code=exc.code)
            _print({"error": {**exc.as_dict(), "trace_id": trace_id}})
            raise typer.Exit(exc.exit_code) from exc
        except Exception as exc:
            log.error("command failed unexpectedly", exc=exc, command=service)
            _print(
                {
                    "error": {
                        "code": "runtime.unexpected",
                        "message": f"{type(exc).__name__}: {exc}",
                        "detail": {},
                        "trace_id": trace_id,
                    }
                }
            )
            raise typer.Exit(2) from exc
    if result is not None:
        _print({**result, "trace_id": trace_id})


@app.command()
def version() -> None:
    """Say what this runtime is and the newest manifest version it reads."""

    def work() -> dict[str, Any]:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as package_version

        from dta_core.manifest.models import MANIFEST_VERSION

        try:
            found = package_version("dta-runtime")
        except PackageNotFoundError:
            log.warn("the runtime's package metadata is missing; its version is not known")
            found = None
        log.info("version asked", version=found, manifest_version=MANIFEST_VERSION)
        return {"package": "dta-runtime", "version": found, "manifest_version": MANIFEST_VERSION}

    _run("version", work)


def _too_old(exc: Any) -> Any:
    """What this runtime answers a manifest newer than it reads with."""
    from dta_core.manifest.models import MANIFEST_VERSION

    from .twin import TwinError

    declared = exc.detail.get("manifest_version")
    log.error(
        "the manifest is newer than this runtime reads",
        manifest_version=declared,
        reads=MANIFEST_VERSION,
    )
    return TwinError(
        f"this runtime reads manifest versions up to {MANIFEST_VERSION}, and the manifest is "
        f"written in version {declared}: run the twin from a newer runtime image",
        code="runtime.too_old",
        detail={"manifest_version": declared, "runtime_reads": MANIFEST_VERSION},
    )


@app.command()
def install(
    bundle: Annotated[
        str, typer.Option(help="File holding the bundle, or - for standard input")
    ] = "-",
    t0: Annotated[
        str | None,
        typer.Option(help="Version time of a first installation, ISO 8601 with offset"),
    ] = None,
) -> None:
    """Create the twin, or bring an existing one in line with a manifest and ontology."""

    def work() -> dict[str, Any]:
        from dta_core.errors import ManifestTooNew

        from .twin import Bundle, TwinError, install_twin

        text = sys.stdin.read() if bundle == "-" else Path(bundle).read_text(encoding="utf-8")
        started = None
        if t0 is not None:
            try:
                started = datetime.fromisoformat(t0)
            except ValueError as exc:
                raise TwinError(
                    f"--t0 is not an ISO 8601 time: {t0!r}", code="twin.bad_time"
                ) from exc
        try:
            given = Bundle.from_json(text)
        except ManifestTooNew as exc:
            raise _too_old(exc) from exc
        installed = install_twin(StoreSettings.from_env(), given, t=started)
        try:
            version = installed.version
            return {
                "actions": list(installed.actions),
                "version": {"id": version.id, "seq": version.seq, "t": version.t.isoformat()},
                "ontology": installed.twin.ontology.hash,
                # Whether sync has written anything yet: a twin that holds data is kept
                # in step by its runs and does not need its history loaded again.
                "loaded": bool(installed.twin.store.versions(cause="sync", limit=1)),
            }
        finally:
            installed.twin.close()

    _run("install", work)


def _manifest_from(option: str) -> Any:
    """The manifest given as YAML text in a file, or on standard input for `-`."""
    import yaml
    from dta_core.errors import ManifestTooNew
    from dta_core.manifest import parse_manifest

    from .twin import TwinError

    origin = "standard input" if option == "-" else option
    try:
        text = sys.stdin.read() if option == "-" else Path(option).read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as exc:
        raise TwinError(
            f"the manifest cannot be read from {origin}: {type(exc).__name__}",
            code="runtime.manifest_unreadable",
        ) from exc
    try:
        return parse_manifest(data, origin=origin)
    except ManifestTooNew as exc:
        raise _too_old(exc) from exc


def _chosen_sources(manifest: Any, only: list[str] | None) -> dict[str, Any]:
    from .twin import TwinError

    declared = manifest.sources or {}
    unknown = sorted(set(only or ()) - set(declared))
    if unknown:
        raise TwinError(
            f"the manifest declares no source {', '.join(unknown)}",
            code="runtime.unknown_source",
            detail={"unknown": unknown, "sources": sorted(declared)},
        )
    return {sid: declared[sid] for sid in sorted(declared) if not only or sid in only}


@app.command()
def probe(
    manifest: Annotated[
        str, typer.Option(help="File holding the manifest, or - for standard input")
    ] = "-",
    source: Annotated[
        list[str] | None, typer.Option(help="Probe only this source; may be repeated")
    ] = None,
) -> None:
    """Read a few rows of every dataset of every source, from where the twin will read them.

    The twin need not be installed. A source that fails does not stop the
    others; the document says which did, and the exit status stays 0.
    """

    def work() -> dict[str, Any]:
        from dta_core.connect import open_connector
        from dta_core.connect import probe as probe_source

        reports: list[dict[str, Any]] = []
        for source_id, entry in _chosen_sources(_manifest_from(manifest), source).items():
            try:
                with open_connector(source_id, entry, os.environ) as connector:
                    reports.append(probe_source(connector).as_dict())
            except DtaError as exc:
                log.warn("source cannot be opened", source=source_id, code=exc.code)
                reports.append(
                    {
                        "source_id": source_id,
                        "ok": False,
                        "error_code": exc.code,
                        "error": exc.message,
                        "datasets": [],
                    }
                )
        ok = bool(reports) and all(report["ok"] for report in reports)
        log.info(
            "probe finished",
            sources=len(reports),
            failed=[report["source_id"] for report in reports if not report["ok"]],
        )
        return {"ok": ok, "sources": reports}

    _run("probe", work)


@app.command()
def profile(
    at: Annotated[
        str, typer.Option(help="Time recorded as the profile's time, ISO 8601 with offset")
    ],
    manifest: Annotated[
        str, typer.Option(help="File holding the manifest, or - for standard input")
    ] = "-",
    source: Annotated[
        list[str] | None, typer.Option(help="Profile only this source; may be repeated")
    ] = None,
    row_cap: Annotated[
        int | None, typer.Option(help="Rows of a dataset the statistics are computed on")
    ] = None,
) -> None:
    """Measure what every source holds and how fast it changes; nothing is written.

    The twin need not be installed. A source that fails does not stop the
    others; the document lists it under `failed`, and the exit status stays 0.
    """

    def work() -> dict[str, Any]:
        import time

        from dta_core.connect import open_connector, profile_source

        from .twin import TwinError

        try:
            profiled_at = datetime.fromisoformat(at)
        except ValueError as exc:
            raise TwinError(f"--at is not an ISO 8601 time: {at!r}", code="twin.bad_time") from exc
        if profiled_at.tzinfo is None:
            raise TwinError("--at needs a UTC offset", code="twin.bad_time")
        options = {} if row_cap is None else {"row_cap": row_cap}
        profiles: dict[str, Any] = {}
        failed: dict[str, Any] = {}
        for source_id, entry in _chosen_sources(_manifest_from(manifest), source).items():
            started = time.perf_counter()
            try:
                with open_connector(source_id, entry, os.environ) as connector:
                    result = profile_source(connector, profiled_at=profiled_at, **options)
            except DtaError as exc:
                log.warn("source cannot be profiled", source=source_id, code=exc.code)
                failed[source_id] = {"code": exc.code, "message": exc.message}
                continue
            profiles[source_id] = result.model_dump(mode="json", exclude_none=True)
            log.info(
                "source profiled",
                source=source_id,
                datasets=len(result.datasets),
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
            )
        return {"ok": bool(profiles) and not failed, "profiles": profiles, "failed": failed}

    _run("profile", work)


@app.command()
def load() -> None:
    """Read every source once and write what is new into the twin."""

    def work() -> dict[str, Any]:
        from .sync import load as run_load
        from .twin import open_twin

        settings = RuntimeSettings.from_env()
        twin = open_twin(settings.store)
        try:
            return run_load(twin, settings, os.environ).as_dict()
        finally:
            twin.close()

    _run("sync", work)


@app.command()
def sync(
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="Keep the run to this source; may be repeated"),
    ] = None,
    replay: Annotated[
        str | None,
        typer.Option(help="Read one dataset again whole, given as <source>.<dataset>"),
    ] = None,
) -> None:
    """One run of a live twin: take the intervals that have ended on the twin's clock."""

    def work() -> dict[str, Any]:
        from .clock import clock_for
        from .sync import Replay, SyncError
        from .sync import run as run_sync
        from .twin import TwinError, open_twin

        again = None
        if replay is not None:
            source_id, _, dataset = replay.partition(".")
            if not source_id or not dataset:
                raise TwinError(
                    f"--replay takes <source>.<dataset>, not {replay!r}",
                    code="runtime.bad_replay",
                )
            again = Replay(source_id, dataset)
        settings = RuntimeSettings.from_env()
        twin = open_twin(settings.store)
        try:
            clock = clock_for(twin.manifest(), os.environ)
            try:
                report = run_sync(
                    twin, settings, os.environ, clock, sources=source or None, replay=again
                )
            finally:
                clock.close()
        finally:
            twin.close()
        if report.status == "failed":
            # Recorded in the twin already; said here in the way every failed command says it.
            error = report.error or {"code": SyncError.code, "message": "the run failed"}
            raise SyncError(
                error["message"], code=error["code"], detail={"report": report.as_dict()}
            )
        return report.as_dict()

    _run("sync", work)


@app.command()
def api(
    host: Annotated[str, typer.Option(help="Address to listen on")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="Port to listen on")] = API_PORT,
) -> None:
    """Serve the twin's HTTP API."""

    def work() -> None:
        import uvicorn

        from .api import create_app
        from .connections import SERVER_KEEP_ALIVE_S

        settings = RuntimeSettings.from_env()
        log.info("API starting", host=host, port=port, keep_alive_s=SERVER_KEEP_ALIVE_S)
        uvicorn.run(
            create_app(settings),
            host=host,
            port=port,
            log_config=None,
            access_log=False,
            timeout_keep_alive=SERVER_KEEP_ALIVE_S,
        )

    _run("api", work)


@app.command()
def ui(
    host: Annotated[str, typer.Option(help="Address to listen on")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="Port to listen on")] = UI_PORT,
) -> None:
    """Serve the browser interface. It reaches the twin only through the API."""

    def work() -> None:
        from waitress import serve

        from .ui import create_app

        settings = UiSettings.from_env()
        log.info("UI starting", host=host, port=port)
        serve(create_app(settings).server, host=host, port=port, threads=8, _quiet=True)

    _run("ui", work)


@app.command()
def scheduler() -> None:
    """Start what is due on the twin's clock by calling the API. Holds no twin logic."""

    def work() -> None:
        import signal
        import threading

        from .scheduler import Scheduler
        from .settings import SchedulerSettings

        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        running = Scheduler(SchedulerSettings.from_env())
        try:
            running.run(stop)
        finally:
            running.close()

    _run("scheduler", work)


@app.command()
def worker(
    host: Annotated[str, typer.Option(help="Address to listen on")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="Port to listen on")] = WORKER_PORT,
) -> None:
    """Run the twin's models and the code registered with it, as the API asks."""

    def work() -> None:
        from .settings import WorkerSettings
        from .worker import serve

        settings = WorkerSettings.from_env()
        log.info("worker starting", host=host, port=port)
        serve(settings, host=host, port=port)

    _run("worker", work)


@app.command()
def mcp(
    host: Annotated[str, typer.Option(help="Address to listen on")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="Port to listen on")] = MCP_PORT,
    probe: Annotated[
        str | None,
        typer.Option(
            help="Instead of serving, check the adapter at this MCP URL as an agent would; "
            'the token is read as {"token": "..."} on standard input'
        ),
    ] = None,
    api_url: Annotated[
        str | None, typer.Option(help="With --probe: the API the adapter stands for")
    ] = None,
) -> None:
    """Serve the twin's API to agents as MCP tools over streamable HTTP. Holds no token."""

    def work() -> dict[str, Any] | None:
        if probe is not None:
            from .mcp.probe import run_probe
            from .twin import TwinError

            if not api_url:
                raise TwinError("--probe needs --api-url", code="runtime.bad_arguments")
            return run_probe(probe, api_url.rstrip("/"), sys.stdin.read())
        from .mcp import McpSettings, serve

        serve(McpSettings.from_env(), host=host, port=port)
        return None

    _run("mcp", work)


@app.command()
def health(
    service: Annotated[str, typer.Argument(help="api, ui, worker or mcp")],
    port: Annotated[int | None, typer.Option(help="Port the service listens on")] = None,
) -> None:
    """Exit 0 when the named service of this container answers; used as its health check."""

    def work() -> dict[str, Any]:
        import httpx

        from .twin import TwinError

        if service not in PORTS:
            raise TwinError(f"unknown service {service!r}", code="runtime.unknown_service")
        target = port or PORTS[service]
        path = "/healthz"
        try:
            response = httpx.get(f"http://127.0.0.1:{target}{path}", timeout=5.0)
        except httpx.HTTPError as exc:
            raise TwinError(
                f"{service} does not answer on port {target}: {type(exc).__name__}",
                code="runtime.unhealthy",
            ) from exc
        if response.status_code != 200:
            raise TwinError(
                f"{service} answered {response.status_code} on {path}", code="runtime.unhealthy"
            )
        return {"service": service, "healthy": True}

    _run("health", work)


if __name__ == "__main__":
    app()
