"""Service settings come from the environment, and a missing or weak one stops the service."""

from __future__ import annotations

import pytest
from dta_core.errors import ConfigError
from dta_runtime.settings import RuntimeSettings, UiSettings, log_file

STORE = {
    "DTA_STORE_HOST": "store",
    "DTA_STORE_PORT": "3306",
    "DTA_STORE_USER": "root",
    "DTA_STORE_PASSWORD": "not-a-real-password",
    "DTA_STORE_DATABASE": "twin",
}
TOKEN = "t" * 32
RUNTIME = {
    **STORE,
    "DTA_API_TOKEN": TOKEN,
    "DTA_ARTIFACTS_DIR": "/var/lib/dta/artifacts",
    "DTA_LOGS_DIR": "/var/log/dta",
}
UI = {"DTA_API_URL": "http://api:8000/", "DTA_API_TOKEN": TOKEN, "DTA_LOGS_DIR": "/var/log/dta"}


def test_runtime_settings_are_read_and_hide_secrets():
    settings = RuntimeSettings.from_env(RUNTIME)
    assert settings.store.database == "twin"
    assert str(settings.artifacts_dir) == "/var/lib/dta/artifacts"
    assert TOKEN not in repr(settings)
    assert "not-a-real-password" not in repr(settings)
    assert log_file(settings.logs_dir, "api").name == "api.jsonl"


@pytest.mark.parametrize("missing", ["DTA_API_TOKEN", "DTA_ARTIFACTS_DIR", "DTA_LOGS_DIR"])
def test_a_missing_runtime_variable_is_named(missing: str):
    environ = {name: value for name, value in RUNTIME.items() if name != missing}
    with pytest.raises(ConfigError) as caught:
        RuntimeSettings.from_env(environ)
    assert caught.value.code == "config.missing_env"
    assert caught.value.detail["missing"] == [missing]


def test_a_short_token_and_a_relative_directory_are_refused():
    for change in ({"DTA_API_TOKEN": "short"}, {"DTA_LOGS_DIR": "logs"}):
        with pytest.raises(ConfigError) as caught:
            RuntimeSettings.from_env({**RUNTIME, **change})
        assert caught.value.code == "config.bad_value"
        assert "short" not in caught.value.message


def test_ui_settings_need_an_http_address():
    settings = UiSettings.from_env(UI)
    assert settings.api_url == "http://api:8000"
    assert TOKEN not in repr(settings)
    with pytest.raises(ConfigError) as caught:
        UiSettings.from_env({**UI, "DTA_API_URL": "api:8000"})
    assert caught.value.code == "config.bad_value"
