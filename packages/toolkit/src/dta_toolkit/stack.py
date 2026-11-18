"""The twin's stack: its compose file and the variables it needs.

The compose file is derived from the manifest and nothing else, so the same
manifest gives the same stack on any machine. What differs between machines
(ports, secrets, where a local source folder is) stays in the workspace's
`.env` and is only referred to by name.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any

from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.manifest.models import ConnectorKind, Tier
from dta_core.sources import PLACEHOLDER, placeholders
from jinja2 import Environment, StrictUndefined

from .envfile import missing, read_env, scrub
from .workerimage import recorded_tag
from .workspace import Workspace

log = Log("stack")

STORE_PASSWORD_ENV = "DTA_STORE_PASSWORD"
COMPOSE_FILE = "compose.yaml"
STORE_SERVICE = "store"
API_SERVICE = "api"
UI_SERVICE = "ui"
SCHEDULER_SERVICE = "scheduler"
WORKER_SERVICE = "worker"
RUNTIME_COMMAND = "dta-runtime"
UP_TIMEOUT_S = 300.0
_COMMAND_TIMEOUT_S = 120.0
_STDERR_TAIL = 2000
# What `docker` itself needs from the caller's environment. Nothing else is
# passed on, so the values compose fills in come from the .env file alone and
# a variable that happens to be set in the caller's shell cannot override it.
_PASSED_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "SSH_AUTH_SOCK",
)
_TEMPLATE = "templates/stack/compose.yaml.j2"
_FILE_SCHEME = "file://"
_DATABASE_MAX = 64
_NETWORK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


class StackError(DtaError):
    """The manifest cannot be turned into a stack, or a command against the stack failed."""

    code = "stack.invalid"
    # Set when a compose command exited with an error: what it printed may still matter.
    completed: subprocess.CompletedProcess[str] | None = None


@dataclass(frozen=True)
class Mount:
    """A folder of this machine made readable to the twin at the same path."""

    host: str  # as written in the compose file, with its variable marked required
    container: str


@dataclass(frozen=True)
class StackPlan:
    twin_id: str
    project: str
    database: str
    store_image: str
    runtime_image: str
    store_password_env: str
    token_env: str
    api_port_env: str
    ui_port_env: str
    source_env: tuple[str, ...]
    source_mounts: tuple[Mount, ...]
    # Networks created by other stacks on this machine, joined to reach their services.
    networks: tuple[str, ...]
    ui: bool
    # A live twin has a scheduler; `scheduler_poll_s` is how often it looks at the clock.
    live: bool
    scheduler_poll_s: float
    # A twin with a model, or with a function supplied as code, has a worker that runs them.
    worker: bool
    worker_image: str
    worker_cpus: float
    worker_memory_mb: int
    # Variables the twin's own code declares; the worker hands each to the code that named it.
    worker_env: tuple[str, ...]
    # Variables the API reads once, at its start: the webhook's address and what the
    # executors of its actions send. Only the API is given them.
    api_env: tuple[str, ...] = ()
    # Digest of everything the API sets up once, at its start, from the manifest; it is a
    # label of the API's container, so a change of it makes compose start the API anew.
    api_start: str = ""

    def required_env(self) -> tuple[str, ...]:
        """Every variable the compose file refuses to start without, in a stable order."""
        names = [self.store_password_env, self.token_env, self.api_port_env]
        if self.ui:
            names.append(self.ui_port_env)
        for name in (
            *self.source_env,
            *self.api_env,
            *(self.worker_env if self.worker else ()),
        ):
            if name not in names:
                names.append(name)
        return tuple(names)


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list | tuple):
        return [text for item in value for text in _strings(item)]
    return []


def _mount(source_id: str, location: str) -> Mount:
    """The folder a local file source needs mounted, as far as it can be named."""
    path = location[len(_FILE_SCHEME) :]
    leading = PLACEHOLDER.match(path)
    if leading is not None:
        # The folder is wherever the variable says; it is mounted at that same path.
        name = leading.group(1)
        return Mount(host=f"${{{name}:?{name} is required}}", container=f"${{{name}}}")
    if not path.startswith("/"):
        log.error("local source is not an absolute path", source=source_id)
        raise StackError(
            f"sources.{source_id}.location must be an absolute path or start with a variable",
            detail={"source": source_id},
        )
    first = PLACEHOLDER.search(path)
    if first is not None:
        # Mount the part of the path that does not depend on a variable.
        path = path[: first.start()].rsplit("/", 1)[0] or "/"
    return Mount(host=path, container=path)


def _networks(names: list[str]) -> tuple[str, ...]:
    for name in names:
        if not _NETWORK_NAME.match(name) or name == "default":
            log.error("network name cannot be used", network=name)
            raise StackError(
                f"runtime.networks: {name!r} is not the name of an existing container network",
                detail={"network": name},
            )
    return tuple(dict.fromkeys(names))


def api_start_config(manifest: Manifest) -> dict[str, Any]:
    """What the API reads from the manifest once, when it starts, and keeps until it stops.

    The notifier is made from the notification's kind and variable, and the executors
    from the add-on and the variables of the actions bound to one; the templates of a
    call are read from the action's record at each call, so they are not part of it.
    """
    contract = manifest.contract
    notification = contract.notification if contract is not None else None
    kind = notification.kind if notification is not None and notification.kind else "log"
    webhook_env = notification.webhook_env if kind == "webhook" and notification else None
    executors = bool(contract is not None and contract.addons.executors)
    executor_env: dict[str, None] = {}
    if executors:
        for _action_id, action in sorted((manifest.actions or {}).items()):
            if action.binding == "executor" and action.executor is not None:
                executor_env.update(dict.fromkeys(action.executor.env))
    return {
        "notification": {"kind": kind, "webhook_env": webhook_env},
        "executors": executors,
        "executor_env": list(executor_env),
    }


def _api_env(config: dict[str, Any]) -> tuple[str, ...]:
    names = dict.fromkeys(config["executor_env"])
    webhook_env = config["notification"]["webhook_env"]
    if webhook_env:
        names.setdefault(webhook_env)
    return tuple(names)


def _digest(config: dict[str, Any]) -> str:
    text = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def plan_stack(manifest: Manifest, worker_image: str | None = None) -> StackPlan:
    """What the stack of this manifest consists of.

    `worker_image` is the image built for the worker's requirements, when the
    manifest names some and it was built; the worker runs the runtime image otherwise.
    """
    if manifest.twin is None or not manifest.twin.id:
        raise StackError("the manifest names no twin id")
    twin_id = manifest.twin.id
    database = ("twin_" + re.sub(r"[^A-Za-z0-9_]", "_", twin_id))[:_DATABASE_MAX]
    env: dict[str, None] = {}
    mounts: dict[Mount, None] = {}
    for source_id, source in sorted((manifest.sources or {}).items()):
        location = source.location or ""
        for name in [*source.env, *placeholders(location)]:
            env.setdefault(name)
        for text in _strings(source.options):
            for name in placeholders(text):
                env.setdefault(name)
        if source.connector == ConnectorKind.file and location.startswith(_FILE_SCHEME):
            mounts.setdefault(_mount(source_id, location))
    functions = manifest.functions or {}
    code = {fid: found for fid, found in functions.items() if found.kind == "registered"}
    worker_env = sorted({name for found in code.values() for name in found.env})
    start_config = api_start_config(manifest)
    interfaces = manifest.interfaces
    plan = StackPlan(
        twin_id=twin_id,
        project=f"dta-{twin_id}",
        database=database,
        store_image=manifest.runtime.store_image,
        runtime_image=manifest.runtime.image,
        store_password_env=STORE_PASSWORD_ENV,
        token_env=interfaces.token_env,
        api_port_env=interfaces.api_port_env,
        ui_port_env=interfaces.ui_port_env,
        source_env=tuple(sorted(env)),
        source_mounts=tuple(mounts),
        networks=_networks(manifest.runtime.networks),
        ui=interfaces.ui,
        live=manifest.contract is not None and manifest.contract.tier == Tier.live,
        scheduler_poll_s=manifest.runtime.scheduler_poll.total_seconds(),
        worker=bool(code) or bool(manifest.models),
        worker_image=worker_image or manifest.runtime.image,
        worker_cpus=manifest.runtime.worker_cpus,
        worker_memory_mb=manifest.runtime.worker_memory_mb,
        worker_env=tuple(worker_env),
        api_env=_api_env(start_config),
        api_start=_digest(start_config),
    )
    log.debug(
        "stack planned",
        project=plan.project,
        database=plan.database,
        source_env=list(plan.source_env),
        api_env=list(plan.api_env),
        api_start=plan.api_start,
        mounts=len(plan.source_mounts),
        ui=plan.ui,
        live=plan.live,
        worker=plan.worker,
    )
    return plan


def render_compose(plan: StackPlan) -> str:
    """The compose file of a planned stack."""
    template = resources.files("dta_toolkit").joinpath(_TEMPLATE).read_text(encoding="utf-8")
    environment = Environment(
        undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True
    )
    return environment.from_string(template).render(
        twin_id=plan.twin_id,
        project=plan.project,
        database=plan.database,
        store_image=plan.store_image,
        runtime_image=plan.runtime_image,
        store_password_env=plan.store_password_env,
        token_env=plan.token_env,
        api_port_env=plan.api_port_env,
        ui_port_env=plan.ui_port_env,
        source_env=plan.source_env,
        source_mounts=plan.source_mounts,
        networks=plan.networks,
        ui=plan.ui,
        live=plan.live,
        scheduler_poll_s=plan.scheduler_poll_s,
        worker=plan.worker,
        worker_image=plan.worker_image,
        worker_cpus=plan.worker_cpus,
        worker_memory_mb=plan.worker_memory_mb,
        worker_env=plan.worker_env,
        api_env=plan.api_env,
        api_start=plan.api_start,
    )


def compose_command(plan: StackPlan, stack_dir: Path, env_file: Path) -> list[str]:
    """The start of every `docker compose` call for this stack."""
    return [
        "docker",
        "compose",
        "--project-name",
        plan.project,
        "--env-file",
        str(env_file),
        "--file",
        str(stack_dir / COMPOSE_FILE),
    ]


class Stack:
    """The stack of one workspace on this machine, and every command run against it.

    All access to a twin's sources and store goes through here: the toolkit
    itself never opens either, it runs the twin's own image inside the stack,
    where addresses, credentials and networks are the ones the twin will use.
    """

    def __init__(self, workspace: Workspace, manifest: Manifest) -> None:
        self.workspace = workspace
        self.plan = plan_stack(manifest, recorded_tag(workspace, manifest))
        self.compose_path = workspace.stack_dir / COMPOSE_FILE
        self.env_file = workspace.env_path
        # Names whose values may be shown: what the manifest declares as not secret, and
        # the ports and the API address. The store's password and the token never are.
        interfaces = manifest.interfaces
        shown = {name for name, entry in manifest.env.items() if not entry.secret}
        shown |= {interfaces.api_port_env, interfaces.ui_port_env, interfaces.base_url_env}
        self._shown_env = shown - {self.plan.store_password_env, self.plan.token_env}

    def secret_values(self) -> dict[str, str]:
        """The values of `.env` to mask in anything shown: all but the declared non-secrets.

        A value that is not a secret (a database name, a folder) is often an
        ordinary word; masking it would blank that word wherever it appears.
        """
        values = read_env(self.env_file)
        return {name: value for name, value in values.items() if name not in self._shown_env}

    def use_worker_image(self, image: str) -> None:
        """Run the worker from `image` from now on; the compose file follows at its next write."""
        if self.plan.worker_image != image:
            log.info("the worker's image changes", image=image, was=self.plan.worker_image)
            self.plan = replace(self.plan, worker_image=image)

    # -- files ---------------------------------------------------------------

    def write(self) -> bool:
        """Bring the compose file in line with the manifest. True when it changed."""
        text = render_compose(self.plan)
        with self.workspace.exclusive():
            if self.compose_path.is_file() and self.compose_path.read_text("utf-8") == text:
                return False
            self.workspace.atomic_write(self.compose_path, text)
        log.info("compose file written", path=str(self.compose_path), project=self.plan.project)
        return True

    def missing_env(self) -> list[str]:
        """Variables the stack needs that the workspace's .env does not set."""
        return missing(self.plan.required_env(), read_env(self.env_file))

    def require_env(self) -> None:
        absent = self.missing_env()
        if absent:
            log.error("the stack's environment is incomplete", missing=absent)
            raise StackError(
                f"{self.env_file.name} does not set {', '.join(absent)}",
                code="stack.env_missing",
                detail={"missing": absent, "env_file": str(self.env_file)},
            )

    # -- docker compose ------------------------------------------------------

    def compose(
        self,
        *args: str,
        input: str | None = None,
        timeout: float | None = _COMMAND_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[str]:
        """Run `docker compose <args>` for this stack and return it when it exits with 0."""
        if shutil.which("docker") is None:
            log.error("docker is not installed or not on the PATH")
            raise StackError("the docker command is not available", code="stack.docker_unavailable")
        self.require_env()
        if not self.compose_path.is_file():
            self.write()
        command = [*compose_command(self.plan, self.workspace.stack_dir, self.env_file), *args]
        environment = {name: os.environ[name] for name in _PASSED_ENV if name in os.environ}
        started = time.perf_counter()
        try:
            done = subprocess.run(
                command,
                input=input,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=environment,
                cwd=self.workspace.root,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            log.error("compose command timed out", args=list(args), timeout_s=timeout)
            raise StackError(
                f"`docker compose {' '.join(args)}` did not finish within {timeout:g} s",
                code="stack.timeout",
                detail={"args": list(args), "timeout_s": timeout},
            ) from exc
        except OSError as exc:
            log.error("docker could not be started", exc=exc)
            raise StackError(
                f"docker could not be started: {type(exc).__name__}",
                code="stack.docker_unavailable",
            ) from exc
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        if done.returncode != 0:
            tail = scrub(done.stderr[-_STDERR_TAIL:], self.secret_values())
            code = "stack.command_failed"
            if "Cannot connect to the Docker daemon" in done.stderr:
                code = "stack.docker_unavailable"
            log.error(
                "compose command failed",
                args=list(args),
                exit_code=done.returncode,
                elapsed_ms=elapsed_ms,
                stderr=tail,
                code=code,
            )
            error = StackError(
                f"`docker compose {' '.join(args)}` failed with exit code {done.returncode}",
                code=code,
                detail={"args": list(args), "exit_code": done.returncode, "stderr": tail},
            )
            error.completed = done  # the caller may still want what was printed
            raise error
        log.debug("compose command finished", args=list(args), elapsed_ms=elapsed_ms)
        return done

    def up(self, *services: str, timeout: float = UP_TIMEOUT_S) -> None:
        """Start the named services (all of them when none is named) and wait until healthy."""
        self.write()
        self.compose("up", "--detach", "--wait", *services, timeout=timeout)
        log.info("stack services are up", services=list(services) or "all")

    def stop(self, *services: str, timeout: float = UP_TIMEOUT_S) -> None:
        """Stop the named services; their containers and the stack's data stay.

        A service that is not running, or was never started, is left as it is.
        """
        if not services:
            raise ValueError("name the services to stop; `down` stops the whole stack")
        self.compose("stop", *services, timeout=timeout)
        log.info("stack services are stopped", services=list(services))

    def down(self, *, volumes: bool = False) -> None:
        """Stop and remove the stack's containers; its data too only when `volumes` is set."""
        extra = ["--volumes"] if volumes else []
        self.compose("down", "--remove-orphans", *extra, timeout=UP_TIMEOUT_S)
        log.info("stack is down", volumes_removed=volumes)

    def services(self) -> list[dict[str, Any]]:
        """State of each container of the stack: service, state and health."""
        done = self.compose("ps", "--all", "--format", "json")
        rows: list[dict[str, Any]] = []
        for line in done.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parsed = json.loads(line)
            for entry in parsed if isinstance(parsed, list) else [parsed]:
                rows.append(
                    {
                        "service": entry.get("Service"),
                        "state": entry.get("State"),
                        "health": entry.get("Health") or None,
                        "container": entry.get("Name"),
                        # Changes when compose made the container anew.
                        "id": entry.get("ID"),
                    }
                )
        return sorted(rows, key=lambda row: str(row["service"]))

    def logs(self, service: str | None = None, *, tail: int = 200) -> str:
        """The last lines the stack's containers wrote, values of the .env file masked."""
        names = [service] if service else []
        done = self.compose("logs", "--no-color", "--tail", str(tail), *names)
        return scrub(done.stdout, self.secret_values())

    # -- the twin's own commands --------------------------------------------

    def runtime(
        self,
        *args: str,
        input: str | None = None,
        store: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run `dta-runtime <args>` in a one-off container of the stack; its JSON document.

        With `store` the store is started first, as the command needs it; without,
        nothing else is started, which is all a look at the sources needs.
        A command that reports an error raises it under the runtime's own code.
        """
        self.write()
        flags = ["--rm", "-T"] if store else ["--rm", "-T", "--no-deps"]
        failed: StackError | None = None
        try:
            done = self.compose(
                "run", *flags, API_SERVICE, RUNTIME_COMMAND, *args, input=input, timeout=timeout
            )
            stdout = done.stdout
        except StackError as exc:
            if exc.completed is None:
                raise
            failed, stdout = exc, exc.completed.stdout
        document = _last_document(stdout)
        if document is None:
            if failed is not None:
                raise failed
            log.error("runtime command printed no document", command=args[0] if args else None)
            raise StackError(
                f"`{RUNTIME_COMMAND} {' '.join(args)}` printed no JSON document",
                code="stack.bad_output",
                detail={"args": list(args)},
            )
        reported = reported_error(document)
        if reported is not None:
            log.error(
                "runtime command reported an error",
                command=args[0] if args else None,
                code=reported.get("code"),
                trace_id=reported.get("trace_id"),
            )
            raise StackError(
                str(reported.get("message") or "the command failed"),
                code=str(reported.get("code") or "stack.runtime_failed"),
                detail={
                    "command": args[0] if args else None,
                    "trace_id": reported.get("trace_id"),
                    **(reported.get("detail") or {}),
                },
            ) from failed
        if failed is not None:
            raise failed
        return document


def reported_error(document: dict[str, Any]) -> dict[str, Any] | None:
    """The error a runtime command reported, if its document is one.

    A report of work done may carry an `error` entry that is empty; only an
    entry that holds an error makes the document an error.
    """
    reported = document.get("error")
    return reported if isinstance(reported, dict) and reported else None


def _last_document(stdout: str) -> dict[str, Any] | None:
    """The JSON object on the last non-empty line of a command's standard output."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None
