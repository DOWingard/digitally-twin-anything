"""The add-ons on a running twin: each is built, used from where its user stands, and
leaves nothing behind once it is no longer selected."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pymysql
import pytest
from dta_toolkit.stack import Stack
from dta_toolkit.workspace import Workspace
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from m3tk_kit import needs_stack
from m8_kit import AddonTwin, code_block, install_into_fresh_venv, port_open, run_in

from conftest import Dta

pytestmark = needs_stack

ALL = {"sdk_python": True, "mcp": True, "readonly_sql": True}
NONE = {"sdk_python": False, "mcp": False, "readonly_sql": False}
READER = "twin_reader"
REFUSED = (1044, 1045, 1142, 1227, 1370)

# Run in a fresh environment holding only the generated package.
USE_SDK = r"""
import json, sys
import pyarrow as pa
package = __import__(sys.argv[1])
report = {}
with package.Twin() as twin:
    head = twin.head()
    report["head"] = head
    for name, model in package.TYPES.items():
        objects = twin.objects(name)
        rows = objects.list(at=head)
        assert rows and all(isinstance(row, model) for row in rows), name
        first = objects.history(rows[0].uid, at=head)
        earlier = objects.list(at=first[0].version)
        assert earlier and all(isinstance(row, model) for row in earlier), name
        table = objects.table(at=head)
        assert isinstance(table, pa.Table) and table.num_rows == len(rows), name
        report[name] = {"head": len(rows), "earlier": len(earlier), "at": first[0].version,
                        "columns": table.column_names}
    sensor = twin.sensor.get("sensor:n1", at=head)
    report["sensor_type"] = type(sensor).__name__
    changes = twin.sensor.history("sensor:n1", at=head, properties=["level"])
    report["levels"] = [c.values.get("level") for c in changes]
    report["first_level"] = twin.sensor.get("sensor:n1", at=changes[0].version).level
    links = twin.sensor.links("sensor:n1", at=head)
    report["links"] = [[l.direction, l.edge_type, l.neighbor] for l in links]
    site = twin.site.get(next(l.neighbor for l in links if l.neighbor_type == "Site"), at=head)
    report["site"] = [type(site).__name__, site.name]
    answered = twin.questions.q_level(at=head, property="Sensor.level")
    report["answer"] = answered.to_dict()
    try:
        twin.sensor.get("sensor:none", at=head)
    except package.TwinApiError as exc:
        report["missing_object"] = [exc.status, exc.code]
for name in (package.twin.BASE_URL_ENV, package.twin.TOKEN_ENV):
    import os
    environ = dict(os.environ)
    environ.pop(name)
    try:
        package.Twin(environ=environ)
    except package.TwinConfigError as exc:
        report.setdefault("config_errors", []).append([exc.missing, str(exc)])
print(json.dumps(report))
"""


@pytest.fixture
def addon_twin(tmp_path: Path, home: Path) -> Iterator[Callable[..., AddonTwin]]:
    made: list[AddonTwin] = []

    def make(**addons: bool) -> AddonTwin:
        base = tmp_path / f"twin{len(made)}"
        base.mkdir()
        twin = AddonTwin(Dta(base / "ws"), base, addons)
        made.append(twin)
        return twin

    try:
        yield make
    finally:
        for twin in made:
            twin.teardown()


def _reader(port: int, password: str, database: str | None = None) -> pymysql.Connection:
    return pymysql.connect(
        host="127.0.0.1",
        port=port,
        user=READER,
        password=password,
        database=database,
        autocommit=False,
        connect_timeout=10,
    )


def _error(connection: pymysql.Connection, sql: str) -> tuple[int, str] | None:
    """The error the statement was refused with; None when it ran."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            cursor.fetchall()
        return None
    except pymysql.err.MySQLError as exc:
        return exc.args[0], str(exc.args[1:])
    finally:
        connection.rollback()


def _denied(error: tuple[int, str] | None) -> bool:
    """Refused for want of a privilege, not for any other reason."""
    return error is not None and (error[0] in REFUSED or "denied" in error[1].lower())


def _rows(connection: pymysql.Connection, sql: str) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return list(cursor.fetchall())


async def _mcp(url: str, headers: dict[str, str]) -> dict[str, Any]:
    async with Client(StreamableHttpTransport(url, headers=headers)) as client:
        started = time.perf_counter()
        tools = await client.list_tools()
        list_ms = (time.perf_counter() - started) * 1000.0
        name = next(tool.name for tool in tools if tool.name.endswith("_api_v1_meta_health_get"))
        health = await client.call_tool(name, {}, raise_on_error=False)
        return {
            "tools": [tool.name for tool in tools],
            "list_ms": list_ms,
            "error": health.is_error,
            "health": health.structured_content,
        }


async def _mcp_refused(url: str) -> str:
    try:
        async with Client(StreamableHttpTransport(url)) as client:
            await client.list_tools()
    except Exception as exc:  # the client raises what the server answered
        return str(exc)
    return "accepted"


def test_every_addon_is_built_used_and_then_removed(addon_twin, tmp_path: Path) -> None:
    twin = addon_twin(**ALL)
    twin.prepare()
    env = twin.env()
    password = env["DTA_SQL_READER_PASSWORD"]
    assert re.fullmatch(r"[A-Za-z0-9_.~+/=@%^:,!#*-]{16,200}", password)
    assert password != env["DTA_STORE_PASSWORD"]

    built = twin.ok("build")
    steps = {entry["step"]: entry for entry in built["steps"]}
    assert [entry["step"] for entry in built["steps"]][-3:] == ["mcp", "skill", "sdk"]
    assert steps["mcp"]["status"] == "ok", steps["mcp"]
    assert steps["sdk"]["status"] == "ok", steps["sdk"]
    sdk = steps["sdk"]["detail"]
    assert sdk["action"] == "generated" and sdk["package"] == "twin_" + twin.id.replace("-", "_")
    assert sdk["timings_ms"]["total_ms"] <= 60_000, sdk["timings_ms"]
    print("sdk generation", sdk["timings_ms"])
    reader = steps["store"]["detail"]["reader"]
    assert reader["present"] and reader["published"], reader

    # A second build generates nothing and grants the same.
    again = {entry["step"]: entry for entry in twin.ok("build")["steps"]}
    assert again["sdk"]["detail"]["action"] == "current"

    accepted = twin.ok("accept")
    checks = {check["id"]: check for check in accepted["checks"]}
    for ident in ("sdk.usable", "mcp.reachable", "sql.readonly"):
        assert checks[ident]["passed"], checks[ident]
        assert checks[ident]["evidence"].get("applicable", True), checks[ident]

    # The client package: installed alone into a fresh environment, it reads the twin.
    sdk_root = twin.root / "sdk" / "python"
    token = env["DTA_API_TOKEN"]
    for path in sdk_root.rglob("*"):
        if path.is_file():
            assert token not in path.read_text(encoding="utf-8", errors="replace"), path
    python = install_into_fresh_venv(sdk_root, tmp_path / "client")
    access = {"DTA_BASE_URL": env["DTA_BASE_URL"], "DTA_API_TOKEN": token}
    done = run_in(python, USE_SDK, access, sdk["package"])
    assert done.returncode == 0, done.stderr[-3000:]
    used = json.loads(done.stdout.strip().splitlines()[-1])
    assert used["Sensor"]["head"] == 3 and used["Site"]["head"] == 2, used
    assert used["sensor_type"] == "Sensor" and used["site"] == ["Site", "North works"]
    assert used["levels"] and used["first_level"] != used["levels"][-1], used
    assert any(link[2] == "site:north" for link in used["links"]), used["links"]
    assert used["missing_object"][0] == 404, used
    answer = used["answer"]
    assert answer["version"]["id"] == used["head"], answer
    assert {row["uid"] for row in answer["value"]} >= {"sensor:n1"}, answer
    assert [error[0] for error in used["config_errors"]] == [["DTA_BASE_URL"], ["DTA_API_TOKEN"]]
    assert token not in done.stdout + done.stderr

    # The skill's own statements, run as they are written.
    skill = (twin.root / "skill" / f"twin-{twin.id}" / "SKILL.md").read_text(encoding="utf-8")
    example = code_block(skill, "python", "### Python client")
    done = run_in(python, example, access)
    assert done.returncode == 0, done.stderr[-3000:]

    # Read-only SQL on the published port.
    sql_port = int(env["DTA_SQL_PORT"])
    database = "twin_" + twin.id.replace("-", "_")
    first = used["Sensor"]["at"]
    connection = _reader(sql_port, password, database)
    try:
        assert {row[0] for row in _rows(connection, "SHOW TABLES")} >= {"sensor", "site"}
        assert len(_rows(connection, "SELECT * FROM sensor")) == 3
        assert len(_rows(connection, f"SELECT * FROM sensor AS OF '{first}'")) >= 1
        assert _rows(connection, "SELECT commit_hash FROM dolt_log LIMIT 1")
        assert _rows(connection, "SELECT * FROM dolt_diff_sensor LIMIT 1")
        assert len(_rows(connection, f"SELECT * FROM `{database}/main`.sensor")) == 3
        for statement in code_block(skill, "sql", "### Read-only SQL").strip().splitlines():
            statement = (
                statement.replace("<table>", "sensor")
                .replace("<version id>", used["head"])
                .replace("<branch>", "main")
            )
            _rows(connection, statement)
        denied = {
            "insert": "INSERT INTO site (uid, name) VALUES ('site:probe', 'probe')",
            "update": "UPDATE site SET name = 'probe'",
            "delete": "DELETE FROM site",
            "create table": "CREATE TABLE probe (a INT)",
            "drop table": "DROP TABLE site",
            "alter table": "ALTER TABLE site ADD COLUMN probe INT",
            "create database": "CREATE DATABASE probe",
            "set global": "SET GLOBAL max_connections = 10",
            "mysql database": "SELECT * FROM mysql.user",
            "grant": f"GRANT ALL ON *.* TO '{READER}'@'%'",
        }
        procedures = {
            "commit": "CALL dolt_commit('--allow-empty', '-m', 'probe')",
            "branch": "CALL dolt_branch('probe')",
            "checkout": "CALL dolt_checkout('-b', 'probe')",
            "reset": "CALL dolt_reset('--hard')",
            "merge": "CALL dolt_merge('main')",
            "tag": "CALL dolt_tag('probe')",
            "gc": "CALL dolt_gc()",
            "backup": "CALL dolt_backup('add', 'probe', 'file:///tmp/probe')",
            "purge": "CALL dolt_purge_dropped_databases()",
        }
        refused = {name: _error(connection, sql) for name, sql in {**denied, **procedures}.items()}
        print("refused", refused)
        assert all(_denied(error) for error in refused.values()), refused
    finally:
        connection.close()

    # The MCP adapter, registered as the skill says.
    registration = json.loads(
        code_block(skill, "json", "### MCP tools")
        .replace("${DTA_MCP_PORT}", env["DTA_MCP_PORT"])
        .replace("${DTA_API_TOKEN}", token)
    )
    (server,) = registration["mcpServers"].values()
    reached = asyncio.run(_mcp(server["url"], server["headers"]))
    assert reached["list_ms"] <= 2_000 and len(reached["tools"]) > 50, reached["list_ms"]
    assert not reached["error"] and reached["health"]["twin_id"] == twin.id, reached
    assert "401" in asyncio.run(_mcp_refused(server["url"]))

    # Off again: each add-on is gone after the next build.
    twin.revise(**NONE)
    built = twin.ok("build")
    steps = {entry["step"]: entry for entry in built["steps"]}
    assert steps["mcp"]["status"] == "skipped" and "removed" in steps["mcp"]["detail"]["reason"]
    assert steps["sdk"]["status"] == "skipped" and "removed" in steps["sdk"]["detail"]["reason"]
    assert steps["store"]["detail"]["reader"] == {
        "user": READER,
        "present": False,
        "published": False,
    }
    assert not (twin.root / "sdk").exists()
    assert not (twin.root / ".dta" / "sdk.json").exists()
    workspace = Workspace(twin.root)
    stack = Stack(workspace, workspace.load_manifest())
    assert "mcp" not in {row["service"] for row in stack.services()}
    assert not port_open(int(env["DTA_MCP_PORT"])) and not port_open(sql_port)
    users = stack.store_sql(f"SELECT user FROM mysql.user WHERE user = '{READER}';")
    assert not (users[0].get("rows") if users else None), users
    twin.failed("sdk.not_selected", "generate", "sdk")
    accepted = twin.ok("accept")
    checks = {check["id"]: check for check in accepted["checks"]}
    for ident in ("sdk.usable", "mcp.reachable", "sql.readonly"):
        assert checks[ident]["passed"] and checks[ident]["evidence"]["applicable"] is False

    assert twin.leaked() == []
