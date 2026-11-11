"""`dta-runtime probe` and `profile`: source access from where the twin runs.

Both take the manifest as text, need no installed twin, and print one JSON
document. The sources here are real CSV files in a temporary folder.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from dta_runtime.cli import app
from typer.testing import CliRunner

SECRET_DIR_VAR = "READINGS_DIR"


def _manifest(extra_sources: dict[str, Any] | None = None) -> str:
    sources: dict[str, Any] = {
        "plant": {
            "connector": "file",
            "location": "file://${READINGS_DIR}",
            "env": [SECRET_DIR_VAR],
            "options": {"datasets": {"readings": {"glob": "readings*.csv", "format": "csv"}}},
        }
    }
    sources.update(extra_sources or {})
    return yaml.safe_dump({"twin": {"id": "plant", "name": "Plant"}, "sources": sources})


@pytest.fixture
def readings(tmp_path: Path) -> Path:
    folder = tmp_path / "source-data"
    folder.mkdir()
    lines = ["sensor,timestamp,value"]
    for step in range(40):
        for sensor in ("a", "b"):
            minute = step * 5
            stamp = f"2026-03-01T{minute // 60:02d}:{minute % 60:02d}:00Z"
            lines.append(f"{sensor},{stamp},{step * (1 if sensor == 'a' else 2)}.5")
    (folder / "readings.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


def _run(args: list[str], manifest: str, env: dict[str, str]) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(app, args, input=manifest, env=env)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, result.output  # exactly one document on standard output
    return result.exit_code, json.loads(lines[0]), result.output


def test_probe_reports_every_dataset_without_row_values(readings: Path):
    code, document, output = _run(["probe"], _manifest(), {SECRET_DIR_VAR: str(readings)})
    assert code == 0 and document["ok"] is True
    (source,) = document["sources"]
    assert source["source_id"] == "plant" and source["ok"] is True
    (dataset,) = source["datasets"]
    assert dataset["dataset"] == "readings" and dataset["has_rows"] is True
    assert [column["name"] for column in dataset["columns"]] == ["sensor", "timestamp", "value"]
    assert "timestamp" in dataset["columns"][1]["type"]
    assert str(readings) not in output  # the filled-in location is never shown
    assert document["trace_id"]


def test_probe_names_the_failing_source_and_still_probes_the_others(readings: Path):
    manifest = _manifest(
        {
            "missing": {
                "connector": "file",
                "location": "file://${ABSENT_DIR}",
                "env": ["ABSENT_DIR"],
                "options": {"datasets": {"x": {"glob": "*.csv", "format": "csv"}}},
            }
        }
    )
    code, document, _ = _run(["probe"], manifest, {SECRET_DIR_VAR: str(readings)})
    assert code == 0 and document["ok"] is False
    by_id = {source["source_id"]: source for source in document["sources"]}
    assert by_id["plant"]["ok"] is True
    assert by_id["missing"]["ok"] is False
    assert by_id["missing"]["error_code"] == "config.missing_env"
    assert "ABSENT_DIR" in by_id["missing"]["error"]


def test_probe_of_one_source_and_of_an_unknown_one(readings: Path):
    env = {SECRET_DIR_VAR: str(readings)}
    code, document, _ = _run(["probe", "--source", "plant"], _manifest(), env)
    assert code == 0 and [s["source_id"] for s in document["sources"]] == ["plant"]
    code, document, _ = _run(["probe", "--source", "nope"], _manifest(), env)
    assert code != 0
    assert document["error"]["code"] == "runtime.unknown_source"
    assert document["error"]["detail"]["sources"] == ["plant"]


def test_profile_measures_the_series_and_keeps_the_given_time(readings: Path):
    at = "2026-03-02T00:00:00+00:00"
    code, document, output = _run(
        ["profile", "--at", at], _manifest(), {SECRET_DIR_VAR: str(readings)}
    )
    assert code == 0 and document["ok"] is True and document["failed"] == {}
    profile = document["profiles"]["plant"]
    assert profile["profiled_at"].startswith("2026-03-02T00:00:00")
    assert profile["refresh_period"] == "PT5M"
    dataset = profile["datasets"]["readings"]
    assert dataset["rows"] == 80
    assert dataset["time_column"] == "timestamp"
    assert dataset["entity_columns"] == ["sensor"]
    assert ["sensor", "timestamp"] in dataset["candidate_keys"]
    assert str(readings) not in output


def test_profile_lists_a_failing_source_and_keeps_the_rest(readings: Path):
    manifest = _manifest(
        {
            "empty": {
                "connector": "file",
                "location": "file://${READINGS_DIR}",
                "env": [SECRET_DIR_VAR],
                "options": {"datasets": {"none": {"glob": "absent*.csv", "format": "csv"}}},
            }
        }
    )
    code, document, _ = _run(
        ["profile", "--at", "2026-03-02T00:00:00Z"], manifest, {SECRET_DIR_VAR: str(readings)}
    )
    assert code == 0 and document["ok"] is False
    assert set(document["profiles"]) == {"plant"}
    assert document["failed"]["empty"]["code"].startswith("source.")


@pytest.mark.parametrize("at", ["yesterday", "2026-03-02T00:00:00"])
def test_profile_refuses_a_time_without_an_offset(readings: Path, at: str):
    code, document, _ = _run(["profile", "--at", at], _manifest(), {SECRET_DIR_VAR: str(readings)})
    assert code != 0 and document["error"]["code"] == "twin.bad_time"


def test_an_unreadable_manifest_is_an_error_document(readings: Path):
    code, document, _ = _run(["probe"], "sources: [this is: not yaml", {})
    assert code != 0 and document["error"]["code"] == "runtime.manifest_unreadable"
