"""Acceptance of the add-ons: each is checked from where its user stands, and a check of
an add-on that is not selected passes as not applicable.

- `sdk.usable`: the generated client installs into a fresh environment, imports, reads
  one object of each type and answers one question.
- `mcp.reachable`: the adapter answers on its published port; its tools are the API's
  operations; a read tool answers with the token and is refused without it.
- `sql.readonly`: the reader signs in on the published port, reads a table at head and
  as of the first version, and is refused an INSERT, DDL and `CALL dolt_commit`.

The checks that stand on this machine's network run a one-off container of the
twin's own images there; the toolkit holds no MCP client and no store driver.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.schema import Ontology

from .. import addons
from ..envfile import scrub
from .client import ApiUnavailable, TwinApi, probe_health, service_url
from .result import CheckResult

log = Log("acceptance.addons")

API_CONTAINER_PORT = 8000
_INSTALL_TIMEOUT_S = 600.0
_RUN_TIMEOUT_S = 300.0
_OUTPUT_TAIL = 2000

# Runs inside a fresh environment that holds the generated package and nothing else.
SDK_PROBE = r"""
import importlib, json, sys
package = importlib.import_module(sys.argv[1])
report = {"types": {}, "question": None}
with package.Twin() as twin:
    accessors = {a.type_name: a for a in vars(twin).values() if isinstance(a, package.Objects)}
    for name, model in package.TYPES.items():
        rows = accessors[name].list(limit=1)
        entry = {"listed": len(rows), "typed": all(isinstance(r, model) for r in rows)}
        if rows:
            one = accessors[name].get(rows[0].uid)
            entry["read"] = isinstance(one, model) and one.uid == rows[0].uid
        report["types"][name] = entry
    if package.QUESTIONS:
        answer = twin.questions.ask(package.QUESTIONS[0])
        report["question"] = {"id": package.QUESTIONS[0], "type": type(answer).__name__}
print(json.dumps(report))
"""

# Runs in the twin's runtime image, which holds a MySQL driver, on this machine's network.
SQL_PROBE = r"""
import json, os, sys
import pymysql
port, database, table, first = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
password = os.environ["DTA_SQL_READER_PASSWORD"]
report = {}
def connect(user, secret, db=None):
    return pymysql.connect(host="127.0.0.1", port=port, user=user, password=secret,
                           database=db, autocommit=True, connect_timeout=10)
try:
    reader = connect("twin_reader", password, database)
except pymysql.err.MySQLError as exc:
    print(json.dumps({"signed_in": False, "error": exc.args[0] if exc.args else None}))
    raise SystemExit(0)
report["signed_in"] = True
def run(name, sql):
    try:
        with reader.cursor() as cursor:
            cursor.execute(sql)
            report[name] = {"ok": True, "rows": len(cursor.fetchall())}
    except pymysql.err.MySQLError as exc:
        report[name] = {"ok": False, "error": exc.args[0] if exc.args else None}
run("read_head", f"SELECT COUNT(*) FROM `{table}`")
run("read_first", f"SELECT COUNT(*) FROM `{table}` AS OF '{first}'")
run("insert", f"INSERT INTO `{table}` (uid) VALUES ('dta-acceptance-probe')")
run("ddl", "CREATE TABLE dta_acceptance_probe (a INT)")
run("dolt_commit", "CALL dolt_commit('--allow-empty', '-m', 'dta acceptance probe')")
try:
    connect("root", password).close()
    report["root_with_reader_password"] = "accepted"
except pymysql.err.MySQLError as exc:
    report["root_with_reader_password"] = exc.args[0] if exc.args else "refused"
print(json.dumps(report))
"""


@dataclass(frozen=True)
class AddonAccess:
    """What the add-on checks reach beyond the API."""

    # The generated package's directory, `<workspace>/sdk/python`.
    sdk_root: Path
    # Runs an image once on this machine's network and returns its JSON document.
    run_image: Callable[..., dict[str, Any]]
    runtime_image: str
    database: str


def _not_applicable(ident: str, examined: str, field: str) -> CheckResult:
    return CheckResult(
        ident, examined, True, {"applicable": False, "reason": f"contract.addons.{field} is false"}
    )


# -- sdk.usable -------------------------------------------------------------------


def _installer(venv: Path) -> tuple[str, list[list[str]]]:
    """How to make a fresh environment and install into it: uv when it is on the PATH."""
    python = str(venv / "bin" / "python")
    uv = shutil.which("uv")
    if uv:
        return "uv", [
            [uv, "venv", "--quiet", "--python", sys.executable, str(venv)],
            [uv, "pip", "install", "--quiet", "--python", python],
        ]
    return "pip", [
        [sys.executable, "-m", "venv", str(venv)],
        [python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check"],
    ]


def _run(
    command: list[str], env: dict[str, str], timeout: float, cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd, check=False
    )


def sdk_usable(manifest: Manifest, values: Mapping[str, str], access: AddonAccess) -> CheckResult:
    ident = "sdk.usable"
    examined = (
        "the generated client installs into a fresh environment, imports, reads one object "
        "of each type and answers one question"
    )
    remedy = "Run dta generate sdk (or dta build) and read the evidence's output."
    if not addons.selected(manifest).sdk_python:
        return _not_applicable(ident, examined, "sdk_python")
    if not (access.sdk_root / "pyproject.toml").is_file():
        return CheckResult(ident, examined, False, {"missing": str(access.sdk_root)}, remedy)
    interfaces = manifest.interfaces
    secrets = {interfaces.token_env: values.get(interfaces.token_env, "")}
    timings: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix="dta-sdk-check-") as scratch:
        root = Path(scratch)
        # Building from a copy writes nothing into the workspace.
        source = root / "source"
        shutil.copytree(access.sdk_root, source, ignore=shutil.ignore_patterns("__pycache__"))
        venv = root / "venv"
        tool, (create, install) = _installer(venv)
        base = {name: os.environ[name] for name in ("PATH", "HOME", "LANG") if name in os.environ}
        base["UV_NO_CONFIG"] = "1"
        steps = [("create", create), ("install", [*install, str(source)])]
        for name, command in steps:
            started = time.perf_counter()
            done = _run(command, base, _INSTALL_TIMEOUT_S, root)
            timings[f"{name}_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
            if done.returncode != 0:
                log.error("the client package could not be installed", step=name, tool=tool)
                return CheckResult(
                    ident,
                    examined,
                    False,
                    {
                        "step": name,
                        "installer": tool,
                        "exit_code": done.returncode,
                        "output": scrub(done.stderr[-_OUTPUT_TAIL:], secrets),
                        **timings,
                    },
                    remedy,
                )
        package = next(
            (p.name for p in access.sdk_root.iterdir() if (p / "__init__.py").is_file()), None
        )
        env = {
            "PATH": base.get("PATH", ""),
            interfaces.base_url_env: values.get(interfaces.base_url_env, ""),
            interfaces.token_env: values.get(interfaces.token_env, ""),
        }
        started = time.perf_counter()
        # -I: only the fresh environment's packages, not the caller's directory or variables.
        done = _run(
            [str(venv / "bin" / "python"), "-I", "-c", SDK_PROBE, str(package)],
            env,
            _RUN_TIMEOUT_S,
            root,
        )
        timings["use_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    if done.returncode != 0:
        log.error("the installed client package failed", exit_code=done.returncode)
        return CheckResult(
            ident,
            examined,
            False,
            {
                "installer": tool,
                "package": package,
                "exit_code": done.returncode,
                "output": scrub(done.stderr[-_OUTPUT_TAIL:], secrets),
                **timings,
            },
            remedy,
        )
    report = json.loads(done.stdout.strip().splitlines()[-1])
    types = report.get("types") or {}
    empty = sorted(name for name, entry in types.items() if not entry["listed"])
    wrong = sorted(
        name
        for name, entry in types.items()
        if not entry["typed"] or (entry["listed"] and not entry.get("read"))
    )
    asks = bool((manifest.contract.questions if manifest.contract else None) or {})
    answered = report.get("question") is not None or not asks
    passed = bool(types) and not wrong and answered and len(empty) < len(types)
    return CheckResult(
        ident,
        examined,
        passed,
        {
            "installer": tool,
            "package": package,
            "types": len(types),
            "types_read": len(types) - len(empty),
            "types_without_objects": empty,
            "not_typed": wrong,
            "question": report.get("question"),
            **timings,
        },
        remedy,
    )


# -- mcp.reachable ----------------------------------------------------------------


def mcp_reachable(
    manifest: Manifest, values: Mapping[str, str], access: AddonAccess
) -> CheckResult:
    ident = "mcp.reachable"
    examined = (
        "the MCP adapter answers on its port; its tools are the API's operations; a read "
        "tool answers with the token and is refused without it"
    )
    remedy = "Start the adapter (dta up mcp) and read its log (dta logs mcp)."
    if not addons.selected(manifest).mcp:
        return _not_applicable(ident, examined, "mcp")
    try:
        status = probe_health(service_url(values, addons.MCP_PORT_ENV), attempts=3)
    except ApiUnavailable as exc:
        return CheckResult(
            ident,
            examined,
            False,
            {"failing_request": "GET /healthz (mcp)", "error": exc.message},
            remedy,
        )
    interfaces = manifest.interfaces
    mcp_url = service_url(values, addons.MCP_PORT_ENV) + addons.MCP_PATH
    api_url = service_url(values, interfaces.api_port_env)
    token = values.get(interfaces.token_env, "")
    try:
        report = access.run_image(
            access.runtime_image,
            "dta-runtime",
            "mcp",
            "--probe",
            mcp_url,
            "--api-url",
            api_url,
            input=json.dumps({"token": token}),
            timeout=_RUN_TIMEOUT_S,
        )
    except DtaError as exc:
        log.error("the adapter could not be probed", code=exc.code)
        return CheckResult(
            ident, examined, False, {"health": status, "error": exc.as_dict()}, remedy
        )
    if report.get("error"):
        return CheckResult(
            ident, examined, False, {"health": status, "error": report["error"]}, remedy
        )
    with_token = report.get("with_token") or {}
    without = report.get("without_token") or {}
    wrong = report.get("wrong_token") or {}
    passed = (
        status == 200
        and report.get("tools", 0) > 0
        and not report.get("missing")
        and not report.get("unexpected")
        and bool(with_token.get("ok"))
        and bool(with_token.get("same_as_api"))
        and without.get("ok") is False
        and without.get("status") == 401
        and wrong.get("ok") is False
        and wrong.get("status") == 401
    )
    evidence = {
        "health": status,
        "port_variable": addons.MCP_PORT_ENV,
        "tools": report.get("tools"),
        "expected": report.get("expected"),
        "missing": report.get("missing"),
        "unexpected": report.get("unexpected"),
        "left_out": report.get("excluded"),
        "list_ms": report.get("list_ms"),
        "read_tool": report.get("read_tool"),
        "with_token": with_token,
        "without_token": without,
        "wrong_token": wrong,
    }
    return CheckResult(ident, examined, passed, evidence, remedy)


# -- sql.readonly -----------------------------------------------------------------


def _first_version(api: TwinApi) -> str:
    response = api.get("/api/v1/history/versions", params={"limit": 1})
    response.raise_for_status()
    return str(response.json()["data"][0]["id"])


def sql_readonly(
    manifest: Manifest,
    ontology: Ontology,
    values: Mapping[str, str],
    access: AddonAccess,
    api: TwinApi,
) -> CheckResult:
    ident = "sql.readonly"
    examined = (
        "the reader signs in on the published port, reads a table at head and as of the "
        "first version, and is refused an INSERT, DDL and CALL dolt_commit"
    )
    remedy = "Build again (dta build) so the reader is granted anew, and read the evidence."
    if not addons.selected(manifest).readonly_sql:
        return _not_applicable(ident, examined, "readonly_sql")
    types = ontology.domain_types()
    if not types:
        return CheckResult(ident, examined, False, {"error": "the ontology has no type"}, remedy)
    table = types[0].table
    password = values.get(addons.SQL_READER_PASSWORD_ENV, "")
    root_password = values.get("DTA_STORE_PASSWORD", "")
    first = _first_version(api)
    port = values.get(addons.SQL_PORT_ENV, "")
    try:
        report = access.run_image(
            access.runtime_image,
            "python",
            "-I",
            "-c",
            SQL_PROBE,
            port,
            access.database,
            table,
            first,
            env={addons.SQL_READER_PASSWORD_ENV: password},
            timeout=_RUN_TIMEOUT_S,
        )
    except DtaError as exc:
        log.error("the reader could not be checked", code=exc.code)
        return CheckResult(ident, examined, False, {"error": exc.as_dict()}, remedy)
    refused = {
        name: (report.get(name) or {}).get("ok") is False
        and (report.get(name) or {}).get("error") in (1142, 1044, 1227, 1370)
        for name in ("insert", "ddl", "dolt_commit")
    }
    reads = {name: bool((report.get(name) or {}).get("ok")) for name in ("read_head", "read_first")}
    distinct = bool(password) and password != root_password
    root_refused = report.get("root_with_reader_password") not in (None, "accepted")
    passed = (
        bool(report.get("signed_in"))
        and all(reads.values())
        and all(refused.values())
        and distinct
        and root_refused
    )
    evidence = {
        "user": addons.READER_USER,
        "port_variable": addons.SQL_PORT_ENV,
        "database": access.database,
        "table": table,
        "first_version": first,
        "signed_in": report.get("signed_in"),
        "reads": {name: report.get(name) for name in ("read_head", "read_first")},
        "refused": {name: report.get(name) for name in ("insert", "ddl", "dolt_commit")},
        "reader_password_differs_from_root": distinct,
        "root_with_reader_password": report.get("root_with_reader_password"),
    }
    return CheckResult(ident, examined, passed, evidence, remedy)
