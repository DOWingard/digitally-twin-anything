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
        installed = install_twin(StoreSettings.from_env(), Bundle.from_json(text), t=started)
        try:
            version = installed.version
            return {
                "actions": list(installed.actions),
                "version": {"id": version.id, "seq": version.seq, "t": version.t.isoformat()},
                "ontology": installed.twin.ontology.hash,
            }
        finally:
            installed.twin.close()

    _run("install", work)


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
def api(
    host: Annotated[str, typer.Option(help="Address to listen on")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="Port to listen on")] = API_PORT,
) -> None:
    """Serve the twin's HTTP API."""

    def work() -> None:
        import uvicorn

        from .api import create_app

        settings = RuntimeSettings.from_env()
        log.info("API starting", host=host, port=port)
        uvicorn.run(create_app(settings), host=host, port=port, log_config=None, access_log=False)

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
def health(
    service: Annotated[str, typer.Argument(help="api or ui")],
    port: Annotated[int | None, typer.Option(help="Port the service listens on")] = None,
) -> None:
    """Exit 0 when the named service of this container answers; used as its health check."""

    def work() -> dict[str, Any]:
        import httpx

        from .twin import TwinError

        if service not in ("api", "ui"):
            raise TwinError(f"unknown service {service!r}", code="runtime.unknown_service")
        target = port or (API_PORT if service == "api" else UI_PORT)
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
