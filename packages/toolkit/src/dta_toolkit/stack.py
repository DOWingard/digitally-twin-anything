"""The twin's stack: its compose file and the variables it needs.

The compose file is derived from the manifest and nothing else, so the same
manifest gives the same stack on any machine. What differs between machines
(ports, secrets, where a local source folder is) stays in the workspace's
`.env` and is only referred to by name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.manifest.models import ConnectorKind
from dta_core.sources import PLACEHOLDER, placeholders
from jinja2 import Environment, StrictUndefined

log = Log("stack")

STORE_PASSWORD_ENV = "DTA_STORE_PASSWORD"
COMPOSE_FILE = "compose.yaml"
_TEMPLATE = "templates/stack/compose.yaml.j2"
_FILE_SCHEME = "file://"
_DATABASE_MAX = 64


class StackError(DtaError):
    """The manifest cannot be turned into a stack."""

    code = "stack.invalid"


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
    ui: bool

    def required_env(self) -> tuple[str, ...]:
        """Every variable the compose file refuses to start without, in a stable order."""
        names = [self.store_password_env, self.token_env, self.api_port_env]
        if self.ui:
            names.append(self.ui_port_env)
        for name in self.source_env:
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


def plan_stack(manifest: Manifest) -> StackPlan:
    """What the stack of this manifest consists of."""
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
        ui=interfaces.ui,
    )
    log.debug(
        "stack planned",
        project=plan.project,
        database=plan.database,
        source_env=list(plan.source_env),
        mounts=len(plan.source_mounts),
        ui=plan.ui,
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
        ui=plan.ui,
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
