"""What selecting an add-on asks of the manifest and of `.env`, without a running twin."""

from __future__ import annotations

import re
from typing import Any

import pytest
from dta_toolkit import addons

MCP_PORT = {"description": "MCP adapter port on the host", "secret": False}
SQL_PORT = {"description": "Read-only SQL port on the host", "secret": False}
READER_PASSWORD = {"description": "Password of the store's reader"}
ADDON_NAMES = ("DTA_MCP_PORT", "DTA_SQL_PORT", "DTA_SQL_READER_PASSWORD")


def _findings(document: dict[str, Any]) -> list[tuple[str, str, str]]:
    found = (
        document["result"]["findings"]
        if document["ok"]
        else document["error"]["detail"]["findings"]
    )
    return [(f["code"], f["severity"], f["path"]) for f in found]


def _env(dta: Any) -> dict[str, str]:
    values = {}
    for line in (dta.root / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            name, _, value = line.partition("=")
            values[name] = value
    return values


@pytest.mark.parametrize(
    ("selected", "declared", "missing"),
    [
        ({"mcp": True}, {}, [("DTA_MCP_PORT", "contract.addons.mcp")]),
        (
            {"readonly_sql": True},
            {"DTA_SQL_PORT": SQL_PORT},
            [("DTA_SQL_READER_PASSWORD", "contract.addons.readonly_sql")],
        ),
        (
            {"mcp": True, "readonly_sql": True},
            {},
            [
                ("DTA_MCP_PORT", "contract.addons.mcp"),
                ("DTA_SQL_PORT", "contract.addons.readonly_sql"),
                ("DTA_SQL_READER_PASSWORD", "contract.addons.readonly_sql"),
            ],
        ),
    ],
    ids=["mcp", "sql-password", "both"],
)
def test_a_selected_addon_whose_variables_are_not_declared_fails_validate(
    workspace, complete, selected, declared, missing
):
    complete["contract"]["addons"].update(selected)
    complete["env"].update(declared)
    workspace.write_manifest(complete)
    code, document = workspace.json("manifest", "validate")
    assert code == 1, document
    assert _findings(document) == [(addons.UNDECLARED, "error", path) for _name, path in missing]
    messages = " ".join(f["message"] for f in document["error"]["detail"]["findings"])
    for name, _path in missing:
        assert name in messages


def test_declared_addon_variables_pass_and_are_not_called_unused(workspace, complete):
    complete["contract"]["addons"].update(mcp=True, readonly_sql=True, sdk_python=True)
    complete["env"].update(
        DTA_MCP_PORT=MCP_PORT, DTA_SQL_PORT=SQL_PORT, DTA_SQL_READER_PASSWORD=READER_PASSWORD
    )
    workspace.write_manifest(complete)
    code, document = workspace.json("manifest", "validate")
    assert code == 0, document
    assert document["result"]["findings"] == []


def test_a_variable_declared_for_an_addon_that_is_off_is_unused(workspace, complete):
    complete["env"].update(DTA_MCP_PORT=MCP_PORT)
    workspace.write_manifest(complete)
    code, document = workspace.json("manifest", "validate")
    assert code == 0, document
    assert _findings(document) == [(addons.ENV_UNUSED, "warning", "env.DTA_MCP_PORT")]


def test_env_template_adds_the_addon_variables_only_when_selected(workspace, complete):
    complete["env"].update(
        DTA_MCP_PORT=MCP_PORT, DTA_SQL_PORT=SQL_PORT, DTA_SQL_READER_PASSWORD=READER_PASSWORD
    )
    workspace.write_manifest(complete)
    code, document = workspace.json("env", "template")
    assert code == 0, document
    assert not set(ADDON_NAMES) & set(_env(workspace))

    complete["contract"]["addons"].update(mcp=True, readonly_sql=True)
    workspace.write_manifest(complete)
    code, document = workspace.json("env", "template")
    assert code == 0, document
    assert document["result"]["generated"] == ["DTA_SQL_READER_PASSWORD"]
    assert {"DTA_MCP_PORT", "DTA_SQL_PORT"} <= set(document["result"]["to_fill"])
    values = _env(workspace)
    assert values["DTA_MCP_PORT"] == "" and values["DTA_SQL_PORT"] == ""
    password = values["DTA_SQL_READER_PASSWORD"]
    assert re.fullmatch(r"[A-Za-z0-9_.~+/=@%^:,!#*-]{16,200}", password)
    assert password != values["DTA_STORE_PASSWORD"]
    assert password not in (workspace.root / ".env.example").read_text(encoding="utf-8")
    assert password not in str(document)


def test_generate_sdk_is_refused_while_the_addon_is_off(workspace, complete):
    workspace.write_manifest(complete)
    code, document = workspace.json("generate", "sdk")
    assert code == 1, document
    assert document["error"]["code"] == "sdk.not_selected"


@pytest.mark.parametrize(
    "value",
    ["", "short", "has a blank in it!", "quote'insidethepassword", "back\\slashinthepassword"],
)
def test_a_reader_password_sql_could_not_carry_is_refused_naming_only_the_variable(value):
    with pytest.raises(addons.AddonError) as raised:
        addons.reader_password({addons.SQL_READER_PASSWORD_ENV: value} if value else {})
    assert addons.SQL_READER_PASSWORD_ENV in raised.value.message
    if value:
        assert value not in raised.value.message and value not in str(raised.value.detail)


def test_the_reader_is_granted_select_on_its_database_and_nothing_else():
    statements = addons.grant_reader_sql("twin_plant", "a-good-password-0123")
    assert "GRANT SELECT ON `twin_plant`.* TO 'twin_reader'@'%';" in statements
    assert "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'twin_reader'@'%';" in statements
    assert statements.count("GRANT ") == 2  # the revoke's GRANT OPTION and the one grant
    with pytest.raises(addons.AddonError):
        addons.grant_reader_sql("twin`; DROP DATABASE x; --", "a-good-password-0123")
