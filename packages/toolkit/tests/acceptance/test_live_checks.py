"""The parts of the live checks and of skill.endpoints that need no running twin."""

from __future__ import annotations

import m3tk_kit as kit
import pytest
from dta_core.manifest import manifest_hash, parse_manifest
from dta_toolkit.acceptance import Context
from dta_toolkit.acceptance.endpoints import (
    listed_endpoints,
    served_endpoints,
    skill_endpoints,
)
from dta_toolkit.acceptance.live import sync_freshness, sync_runs, sync_scheduler, sync_sources
from dta_toolkit.commands.syncjobs import (
    clock_origin,
    failing_sources,
    run_body,
    verdict,
)
from dta_toolkit.skill import render_skill


def _live(interval: str = "PT1M") -> dict:
    data = kit.live_manifest("m3tk-00000000")
    data["sync"]["interval"] = interval
    return data


def _api_reference(data: dict) -> str:
    manifest = parse_manifest(data)
    return render_skill(manifest, manifest_hash=manifest_hash(manifest)).files["reference/api.md"]


def test_the_skill_lists_its_endpoints_from_the_first_column_of_its_tables() -> None:
    static = listed_endpoints(_api_reference(kit.manifest("m3tk-00000000")))
    live = listed_endpoints(_api_reference(_live()))
    assert ("GET", "/meta/health") in static
    assert ("GET", "/objects/{type}/{uid}/links") in static  # the query and `\|` cut off
    assert ("POST", "/graph/subgraph") in static
    assert ("POST", "/sync/runs") not in static and ("POST", "/sync/runs") in live
    only_live = set(live) - set(static)
    assert ("POST", "/sync/runs") in only_live
    assert {path.split("/")[1] for _, path in only_live} == {
        "sync",
        "fidelity",
        "actions",
        "control",
    }
    # Parameter names are not endpoints; prose outside the tables is not read.
    assert all(path.startswith("/") for _, path in static)
    assert ("GET", "/healthz") not in static


def test_paths_compare_with_any_parameter_names() -> None:
    document = {
        "paths": {
            "/api/v1/objects/{type_name}/{object_uid}": {"get": {}, "parameters": []},
            "/api/v1/sync/runs": {"get": {}, "post": {}},
        }
    }
    assert served_endpoints(document) == {
        ("GET", "/api/v1/objects/{}/{}"),
        ("GET", "/api/v1/sync/runs"),
        ("POST", "/api/v1/sync/runs"),
    }


def test_skill_endpoints_does_not_apply_without_a_skill() -> None:
    result = skill_endpoints(None, None, "skill/twin-x/reference/api.md")  # type: ignore[arg-type]
    assert result.passed and result.evidence["applicable"] is False


@pytest.mark.parametrize("check", [sync_scheduler, sync_runs, sync_sources, sync_freshness])
def test_sync_checks_do_not_apply_to_a_static_twin(check) -> None:
    manifest = parse_manifest(kit.manifest("m3tk-00000000"))
    ctx = Context(api=None, manifest=manifest, ontology=None)  # type: ignore[arg-type]
    result = check(ctx, list) if check is sync_scheduler else check(ctx)
    assert result.passed and result.evidence == {
        "applicable": False,
        "reason": "contract.tier is static: the twin does not sync",
    }


def _job(state: str, status: str | None, sources: dict | None = None, error=None) -> dict:
    result = None if status is None else {"status": status, "sources": sources or {}}
    return {"id": "j1", "state": state, "result": result, "error": error}


def test_a_run_is_ok_only_when_it_succeeded_for_every_source() -> None:
    ok = {"plant": {"status": "ok", "error": None}}
    assert verdict(_job("succeeded", "succeeded", ok)).ok

    bad = {"code": "sync.source_failed", "message": "permission denied"}
    partial = verdict(
        _job("succeeded", "partial", {"plant": {"status": "failing", "error": bad}, **{}})
    )
    assert not partial.ok and partial.code == "sync.partial"
    assert "source plant (sync.source_failed: permission denied)" in str(partial.message)

    failed = verdict(
        _job(
            "failed",
            "failed",
            {"plant": {"status": "failing", "error": bad}},
            {"code": "sync.source_failed", "message": "every source of the run failed"},
        )
    )
    assert not failed.ok and failed.code == "sync.source_failed"
    assert failed.failing == [
        {
            "source": "plant",
            "status": "failing",
            "code": "sync.source_failed",
            "message": "permission denied",
        }
    ]

    raised = verdict(
        _job("failed", None, error={"code": "clock.unavailable", "message": "no clock"})
    )
    assert not raised.ok and raised.code == "clock.unavailable" and raised.failing == []


def test_what_a_run_is_asked_for_and_where_the_clock_is_read() -> None:
    assert run_body(None, None) == {}
    assert run_body(["a", "b"], None) == {"sources": ["a", "b"]}
    assert run_body(None, ("plant", "readings")) == {
        "replay": {"source": "plant", "dataset": "readings"}
    }
    assert failing_sources(None) == []
    data = _live()
    assert clock_origin(parse_manifest(data)) == "the wall clock of the API's host"
    data["runtime"]["clock"] = "source:plant"
    assert clock_origin(parse_manifest(data)) == "source plant"
