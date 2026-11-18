"""The optional parts of a twin that `contract.addons` selects: a typed Python client
(`sdk_python`), an MCP adapter (`mcp`) and read-only SQL over the store (`readonly_sql`).

An add-on that is not selected leaves nothing behind: no files, no service, no
published port, no store user. Each one that is selected opens exactly what it
names here: the adapter a host port of its own, the SQL access a host port and a
second credential.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.manifest.checks import Finding, Severity

log = Log("addons")

MCP_PORT_ENV = "DTA_MCP_PORT"
SQL_PORT_ENV = "DTA_SQL_PORT"
SQL_READER_PASSWORD_ENV = "DTA_SQL_READER_PASSWORD"
MCP_SERVICE = "mcp"
# Port the adapter listens on inside its container; the runtime's own default.
MCP_CONTAINER_PORT = 8200
MCP_PATH = "/mcp"
READER_USER = "twin_reader"
SDK_DIR = ("sdk", "python")

UNDECLARED = "addons.env_undeclared"
ENV_UNUSED = "ref.env_unused"
# A password goes into a statement of SQL; one with a quote, a backslash or a blank
# in it could end that statement early, so it is refused instead of escaped.
_READER_PASSWORD = re.compile(r"^[A-Za-z0-9_.~+/=@%^:,!#*-]{16,200}$")

ENV_DESCRIPTIONS = {
    MCP_PORT_ENV: ("Host port of the twin's MCP adapter, bound to 127.0.0.1", False),
    SQL_PORT_ENV: ("Host port of read-only SQL over the twin's store, bound to 127.0.0.1", False),
    SQL_READER_PASSWORD_ENV: (f"Password of the store user {READER_USER}, who may only read", True),
}


class AddonError(DtaError):
    code = "addons.invalid"


@dataclass(frozen=True)
class Selected:
    sdk_python: bool
    mcp: bool
    readonly_sql: bool

    def env(self) -> tuple[str, ...]:
        """The variables the selected add-ons need, in a stable order."""
        names: list[str] = []
        if self.mcp:
            names.append(MCP_PORT_ENV)
        if self.readonly_sql:
            names += [SQL_PORT_ENV, SQL_READER_PASSWORD_ENV]
        return tuple(names)

    def generated_env(self) -> tuple[str, ...]:
        """The variables whose values the toolkit makes up."""
        return (SQL_READER_PASSWORD_ENV,) if self.readonly_sql else ()

    def ports(self) -> tuple[str, ...]:
        """The variables that are host ports, shown where values are shown."""
        found = []
        if self.mcp:
            found.append(MCP_PORT_ENV)
        if self.readonly_sql:
            found.append(SQL_PORT_ENV)
        return tuple(found)


def selected(manifest: Manifest) -> Selected:
    addons = manifest.contract.addons if manifest.contract is not None else None
    if addons is None:
        return Selected(False, False, False)
    return Selected(
        sdk_python=bool(addons.sdk_python),
        mcp=bool(addons.mcp),
        readonly_sql=bool(addons.readonly_sql),
    )


def _uses(manifest: Manifest) -> list[tuple[str, str]]:
    """Each variable a selected add-on needs, with the field that selects it."""
    chosen = selected(manifest)
    uses = []
    if chosen.mcp:
        uses.append((MCP_PORT_ENV, "contract.addons.mcp"))
    if chosen.readonly_sql:
        uses.append((SQL_PORT_ENV, "contract.addons.readonly_sql"))
        uses.append((SQL_READER_PASSWORD_ENV, "contract.addons.readonly_sql"))
    return uses


def findings(manifest: Manifest) -> list[Finding]:
    """An error for each variable a selected add-on needs that the manifest does not declare."""
    found = []
    for name, path in _uses(manifest):
        if name not in manifest.env:
            log.debug("add-on variable not declared", name=name, addon=path)
            found.append(
                Finding(
                    code=UNDECLARED,
                    severity=Severity.error,
                    path=path,
                    message=f"variable '{name}' is needed by this add-on but not declared in env",
                )
            )
    return found


def without_false_unused(manifest: Manifest, found: Iterable[Finding]) -> list[Finding]:
    """`found` without the warning that a variable a selected add-on uses is unused.

    The manifest's own checks know the variables of the interfaces, sources,
    functions and actions, but not those of the add-ons.
    """
    used = {f"env.{name}" for name, _path in _uses(manifest)}
    return [f for f in found if not (f.code == ENV_UNUSED and f.path in used)]


def reader_password(values: dict[str, str]) -> str:
    """The reader's password from the environment file, refused when SQL could not carry it."""
    value = values.get(SQL_READER_PASSWORD_ENV) or ""
    if not value:
        log.error("the reader's password is not set", name=SQL_READER_PASSWORD_ENV)
        raise AddonError(
            f"{SQL_READER_PASSWORD_ENV} is not set",
            code="addons.reader_password_missing",
            detail={"name": SQL_READER_PASSWORD_ENV},
        )
    if not _READER_PASSWORD.match(value):
        log.error("the reader's password cannot be used", name=SQL_READER_PASSWORD_ENV)
        raise AddonError(
            f"{SQL_READER_PASSWORD_ENV} must be 16 to 200 characters of letters, digits and "
            "_ . ~ + / = @ % ^ : , ! # * - (no quotes, backslashes or blanks); "
            "`dta env template` generates one when the line is absent",
            code="addons.reader_password_unusable",
            detail={"name": SQL_READER_PASSWORD_ENV},
        )
    return value


def _quoted_name(database: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", database):
        raise AddonError(
            f"database name {database!r} cannot be granted", code="addons.bad_database"
        )
    return f"`{database}`"


def grant_reader_sql(database: str, password: str) -> str:
    """Statements that make the reader exist with this password and only `SELECT` on `database`.

    Each is idempotent, so they are run on every build: the password follows the
    environment file, and whatever else the user was granted is taken away.
    """
    user = f"'{READER_USER}'@'%'"
    return "\n".join(
        [
            f"CREATE USER IF NOT EXISTS {user} IDENTIFIED BY '{password}';",
            f"ALTER USER {user} IDENTIFIED BY '{password}';",
            f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM {user};",
            f"GRANT SELECT ON {_quoted_name(database)}.* TO {user};",
            f"SHOW GRANTS FOR {user};",
            "",
        ]
    )


def drop_reader_sql() -> str:
    return f"DROP USER IF EXISTS '{READER_USER}'@'%';\n"
