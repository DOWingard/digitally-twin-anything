import json
import logging

import pytest
from dta_core import log as dta_log
from dta_core.config import env_int, require_env
from dta_core.errors import ConfigError


def _lines(path):
    for handler in logging.getLogger("dta").handlers:
        handler.flush()
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_log_line_has_required_fields_and_masks_secrets(tmp_path):
    target = tmp_path / "out.log"
    dta_log.configure(log_file=target, level="debug")
    with dta_log.trace("a" * 32):
        dta_log.Log("unit").info("hello", api_key="s3cret", nested={"password": "x", "n": 1})
        try:
            raise ValueError("boom")
        except ValueError as exc:
            dta_log.Log("unit").error("failed", exc=exc, step=2)
    first, second = _lines(target)
    for field in ("timestamp", "level", "context", "message", "trace_id", "error", "stack"):
        assert field in first
    assert first["trace_id"] == "a" * 32 and first["context"] == "unit"
    assert first["data"] == {"api_key": "***", "nested": {"password": "***", "n": 1}}
    assert "s3cret" not in target.read_text()
    assert second["level"] == "error" and second["error"] == "ValueError: boom"
    assert "Traceback" in second["stack"]


def test_unknown_level_falls_back_and_warns(tmp_path):
    target = tmp_path / "out.log"
    dta_log.configure(log_file=target, level="chatty")
    assert _lines(target)[0]["level"] == "warn"


def test_require_env_lists_every_missing_name_and_never_the_values():
    with pytest.raises(ConfigError) as caught:
        require_env(["A", "B", "C"], {"A": "value", "B": ""})
    assert caught.value.code == "config.missing_env"
    assert caught.value.detail == {"missing": ["B", "C"]}
    assert "value" not in str(caught.value)
    assert require_env(["A"], {"A": "value"}) == {"A": "value"}


def test_env_int_rejects_text():
    assert env_int("P", {"P": "42"}) == 42
    with pytest.raises(ConfigError) as caught:
        env_int("P", {"P": "forty-two"})
    assert caught.value.code == "config.bad_value"
