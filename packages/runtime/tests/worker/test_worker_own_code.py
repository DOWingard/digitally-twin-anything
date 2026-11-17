"""Runs of code the twin brought: a zip archive kept as an artifact, run in a fresh
process that sees only what it declared."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from api_testkit import TOKEN, level
from worker_testkit import (
    TwinApi,
    Worker,
    archive,
    function_body,
    problem_of,
    table_of,
    trace_id,
    twin_code,
)

GIB = 1024**3


@pytest.fixture(scope="module")
def code(twin_api: TwinApi) -> str:
    return twin_api.upload(twin_code())


def test_an_entry_of_the_archive_is_called_at_the_version_asked(
    worker: Worker, twin_api: TwinApi, code: str
):
    old = twin_api.water.versions[5]
    body = function_body(
        code, "twin_code:level", old.id, branch=old.branch, args={"uid": "node:T1"}
    )
    result = worker.ok(body)
    assert result["value"] == {"level": level(5), "at": old.id, "read": old.id}
    assert result["duration_ms"] > 0
    assert any(line["message"] == "code unpacked" for line in result["logs"])
    # At the head the same object holds another value.
    head = twin_api.water.head
    later = worker.ok({**body, "at": head.id, "trace_id": trace_id()})
    assert later["value"]["level"] == level(head.seq) != level(5)


def test_the_code_sees_what_it_declared_and_nothing_else(
    worker: Worker,
    twin_api: TwinApi,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    # The worker holds the token and other secrets in its own environment, as in a stack.
    monkeypatch.setenv("DTA_API_TOKEN", TOKEN)
    monkeypatch.setenv("PLANT_SECRET", "s3cret-value")
    monkeypatch.setenv("NOT_DECLARED", "hidden-value")
    # A module in the worker's current directory is not the code's to import.
    (tmp_path / "planted.py").write_text("X = 1\n")
    monkeypatch.chdir(tmp_path)
    head = twin_api.water.head
    body = function_body(
        code, "twin_code:environment", head.id, branch=head.branch, env=["PLANT_SECRET"]
    )
    found = worker.ok(body)["value"]
    environ, initial = found["environ"], found["initial"]
    assert set(environ) == {"PATH", "LANG", "TMPDIR", "PLANT_SECRET"}
    assert environ["PLANT_SECRET"] == "s3cret-value" and environ["LANG"] == "C.UTF-8"
    assert sorted(entry.split("=", 1)[0] for entry in initial.split("\0") if entry) == sorted(
        environ
    )
    for secret in (TOKEN, "hidden-value", os.environ["DTA_STORE_PASSWORD"]):
        assert secret not in repr(environ) and secret not in initial
    # Its own directory for temporary files, removed after the run; it starts there.
    tmpdir = Path(environ["TMPDIR"])
    assert tmpdir.parent == worker.pools.runs_root and found["cwd"] == str(tmpdir)
    assert not tmpdir.exists()
    # Isolated mode: no current directory on the path, neither the worker's nor its own.
    assert found["flags"] == {"isolated": 1, "safe_path": True}
    assert not found["planted"] and not found["mine"]
    assert "" not in found["path"] and str(tmp_path) not in found["path"]
    assert found["path"][0] == str(worker.pools.code_root / code)
    # Its limits, from outside: address space and CPU time; nothing on standard input.
    assert found["limits"]["as"] == [GIB, GIB]
    assert found["limits"]["cpu"] == [30 + 5, 30 + 5 + 2]
    assert found["input"] == ""


def test_code_that_prints_does_not_disturb_its_answer(worker: Worker, twin_api: TwinApi, code: str):
    head = twin_api.water.head
    assert (
        worker.ok(function_body(code, "twin_code:shout", head.id, branch=head.branch))["value"]
        == "said"
    )


def test_a_table_of_the_codes_comes_back_as_arrow(worker: Worker, twin_api: TwinApi, code: str):
    head = twin_api.water.head
    table, result = table_of(
        worker.run(function_body(code, "twin_code:table", head.id, branch=head.branch))
    )
    assert table.to_pydict() == {"uid": ["node:T1"], "level": [1.0]}
    assert result["table"] is True and result["duration_ms"] > 0


def test_a_family_given_as_code_runs_in_a_fresh_process_of_the_batch_pool(
    worker: Worker, twin_api: TwinApi, code: str
):
    body = {
        "op": "fit",
        "budget_s": 30,
        "trace_id": trace_id(),
        "family": {"artifact": code, "entry": "twin_code:family"},
        "params": {"alpha": 0.5},
        "frame": {"target": "Tank.level", "start": "#4", "end": "head", "step": "PT1H"},
        "horizon": 2,
    }
    result = worker.ok(body)
    head = twin_api.water.head
    assert result["notes"] == [
        f'{{"at": "{head.id}", "branch": "{head.branch}", "params": {{"alpha": 0.5}}}}'
    ]


def test_variables_the_worker_lacks_or_keeps_are_refused(
    worker: Worker, twin_api: TwinApi, code: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("DTA_API_TOKEN", TOKEN)
    monkeypatch.delenv("NO_SUCH_VARIABLE", raising=False)
    head = twin_api.water.head
    for names, named in (
        (["NO_SUCH_VARIABLE"], "NO_SUCH_VARIABLE"),
        (["DTA_API_TOKEN"], "DTA_API_TOKEN"),
        (["PATH"], "PATH"),
    ):
        body = function_body(code, "twin_code:noop", head.id, branch=head.branch, env=names)
        document = problem_of(worker.run(body), 400, "worker.bad_request")
        assert document["names"] == [named]
        assert TOKEN not in str(document)


def test_code_whose_content_does_not_match_its_hash_is_refused(worker: Worker, twin_api: TwinApi):
    sha256 = twin_api.upload(twin_code())
    twin_api.blob(sha256).write_bytes(twin_code())
    head = twin_api.water.head
    body = function_body(sha256, "twin_code:noop", head.id, branch=head.branch)
    document = problem_of(worker.run(body), 422, "model.bad_state")
    assert sha256 in document["detail"]
    assert not (worker.pools.code_root / sha256).exists()


@pytest.mark.parametrize(
    ("files", "said"),
    [
        ({"twin_code.py": "X = 1\n", "../escape.py": "X = 2\n"}, "'..'"),
        ({"twin_code.py": "X = 1\n", "/etc/escape.py": "X = 2\n"}, "absolute path"),
    ],
)
def test_an_archive_entry_that_leads_out_is_refused(
    worker: Worker, twin_api: TwinApi, files: dict[str, str], said: str
):
    sha256 = twin_api.upload(archive({**files, "nonce.txt": trace_id()}))
    head = twin_api.water.head
    body = function_body(sha256, "twin_code:noop", head.id, branch=head.branch)
    document = problem_of(worker.run(body), 422, "model.bad_state")
    assert said in document["detail"]
    assert not (worker.pools.code_root / sha256).exists()
    assert not (worker.pools.code_root.parent / "escape.py").exists()


def test_an_artifact_that_is_not_an_archive_is_refused(worker: Worker, twin_api: TwinApi):
    sha256 = twin_api.upload(f"plain text {trace_id()}".encode())
    head = twin_api.water.head
    body = function_body(sha256, "twin_code:noop", head.id, branch=head.branch)
    document = problem_of(worker.run(body), 422, "model.bad_state")
    assert "not a zip archive" in document["detail"]
