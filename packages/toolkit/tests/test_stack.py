"""The compose file of a twin is derived from its manifest alone."""

from __future__ import annotations

import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from dta_core.manifest import Manifest, parse_manifest
from dta_toolkit.stack import (
    STORE_PASSWORD_ENV,
    Mount,
    StackError,
    compose_command,
    plan_stack,
    render_compose,
)


def manifest(**sections: Any) -> Manifest:
    document: dict[str, Any] = {
        "twin": {"id": "water-static", "name": "Water"},
        "sources": {
            "registry": {
                "connector": "sql",
                "location": "postgresql://${REG_USER}:${REG_PASSWORD}@${REG_HOST}/registry",
                "env": ["REG_USER", "REG_PASSWORD", "REG_HOST"],
            },
            "telemetry": {
                "connector": "file",
                "location": "file://${RECORDING_DIR}/telemetry",
                "env": ["RECORDING_DIR"],
            },
            "feeds": {
                "connector": "file",
                "location": "file://${RECORDING_DIR}/feeds",
                "options": {"datasets": {"meters": {"glob": "${METER_GLOB}", "format": "csv"}}},
            },
            "archive": {"connector": "file", "location": "file:///srv/archive/${REGION}/files"},
            "remote": {"connector": "file", "location": "https://example.org/feeds"},
        },
    }
    document.update(sections)
    return parse_manifest(document, origin="test")


def test_the_plan_names_everything_the_stack_needs():
    plan = plan_stack(manifest())
    assert plan.project == "dta-water-static"
    assert plan.database == "twin_water_static"
    assert plan.store_image == "dolthub/dolt-sql-server:2.7.3"
    assert plan.source_env == (
        "METER_GLOB",
        "RECORDING_DIR",
        "REGION",
        "REG_HOST",
        "REG_PASSWORD",
        "REG_USER",
    )
    # One mount per local folder; a remote location needs none.
    assert plan.source_mounts == (
        Mount(host="/srv/archive", container="/srv/archive"),
        Mount(host="${RECORDING_DIR:?RECORDING_DIR is required}", container="${RECORDING_DIR}"),
    )
    assert plan.required_env()[:4] == (
        STORE_PASSWORD_ENV,
        "DTA_API_TOKEN",
        "DTA_API_PORT",
        "DTA_UI_PORT",
    )
    assert set(plan.required_env()[4:]) == set(plan.source_env)


def test_the_compose_file_holds_three_services_and_no_literal_secret():
    text = render_compose(plan_stack(manifest()))
    document = yaml.safe_load(text)
    assert document["name"] == "dta-water-static"
    assert set(document["services"]) == {"store", "api", "ui"}
    assert set(document["volumes"]) == {"store-data", "artifacts", "logs"}
    store, api, ui = (document["services"][name] for name in ("store", "api", "ui"))
    assert "ports" not in store, "the store is reachable only inside the stack"
    assert api["ports"] == ["127.0.0.1:${DTA_API_PORT:?DTA_API_PORT is required}:8000"]
    assert ui["ports"] == ["127.0.0.1:${DTA_UI_PORT:?DTA_UI_PORT is required}:8050"]
    assert api["environment"]["DTA_STORE_DATABASE"] == "twin_water_static"
    assert api["environment"]["REG_PASSWORD"] == "${REG_PASSWORD:?REG_PASSWORD is required}"
    assert "${RECORDING_DIR:?RECORDING_DIR is required}:${RECORDING_DIR}:ro" in api["volumes"]
    assert "/srv/archive:/srv/archive:ro" in api["volumes"]
    # The interface holds the token and reaches the twin through the API, not the store.
    assert set(ui["environment"]) == {"DTA_API_URL", "DTA_API_TOKEN", "DTA_LOGS_DIR"}
    assert api["depends_on"] == {"store": {"condition": "service_healthy"}}
    assert ui["depends_on"] == {"api": {"condition": "service_healthy"}}
    for service in (store, api, ui):
        assert "healthcheck" in service and service["restart"] == "unless-stopped"
    # Every value that could be a secret is a reference to a variable that must be set.
    for service in (store, api, ui):
        for name, value in service["environment"].items():
            if any(word in name for word in ("PASSWORD", "TOKEN", "KEY", "SECRET")):
                assert value.startswith("${") and ":?" in value, name


def test_a_live_twin_has_a_scheduler_that_holds_the_token_and_nothing_else():
    static = yaml.safe_load(render_compose(plan_stack(manifest())))
    assert "scheduler" not in static["services"]

    live = manifest(
        contract={"tier": "live"}, runtime={"scheduler_poll": "PT0.25S", "networks": ["plant-net"]}
    )
    document = yaml.safe_load(render_compose(plan_stack(live)))
    scheduler = document["services"]["scheduler"]
    assert scheduler["command"] == ["dta-runtime", "scheduler"]
    assert scheduler["depends_on"] == {"api": {"condition": "service_healthy"}}
    assert scheduler["environment"] == {
        "DTA_API_URL": "http://api:8000",
        "DTA_API_TOKEN": "${DTA_API_TOKEN:?DTA_API_TOKEN is required}",
        "DTA_LOGS_DIR": "/var/log/dta",
        "DTA_SCHEDULER_POLL_S": "0.25",
    }
    # It reaches the twin through the API only: no port, no source network, no source folder.
    assert "ports" not in scheduler and "networks" not in scheduler
    assert scheduler["volumes"] == ["logs:/var/log/dta"]
    assert scheduler["restart"] == "unless-stopped"


CODE = {"kind": "registered", "entry": "plant.hydraulics:simulate", "env": ["SOLVER_LICENCE"]}


def test_a_twin_with_models_or_its_own_code_has_a_worker_that_reaches_only_the_api():
    plain = plan_stack(manifest())
    assert not plain.worker and "worker" not in yaml.safe_load(render_compose(plain))["services"]
    builtin = plan_stack(manifest(functions={"f": {"kind": "builtin", "entry": "latest"}}))
    assert not builtin.worker
    assert plan_stack(manifest(models={"m_level": {"question": "q_level"}})).worker

    plan = plan_stack(
        manifest(
            functions={"f_sim": CODE},
            runtime={"networks": ["plant-net"], "worker_cpus": 2, "worker_memory_mb": 2048},
        )
    )
    assert plan.worker and plan.worker_env == ("SOLVER_LICENCE",)
    assert "SOLVER_LICENCE" in plan.required_env()
    document = yaml.safe_load(render_compose(plan))
    worker = document["services"]["worker"]
    assert worker["command"] == ["dta-runtime", "worker"]
    assert worker["image"] == document["services"]["api"]["image"]
    assert worker["environment"] == {
        "DTA_API_URL": "http://api:8000",
        "DTA_API_TOKEN": "${DTA_API_TOKEN:?DTA_API_TOKEN is required}",
        "DTA_WORKER_CPUS": "2.0",
        "DTA_WORKER_MEMORY_MB": "2048",
        "SOLVER_LICENCE": "${SOLVER_LICENCE:?SOLVER_LICENCE is required}",
    }
    # Its only network has no way out; it has no port, no volume and no privilege to gain.
    assert worker["networks"] == ["compute"]
    assert document["networks"]["compute"] == {"internal": True}
    assert "ports" not in worker and "volumes" not in worker
    assert worker["read_only"] is True and worker["cap_drop"] == ["ALL"]
    assert worker["security_opt"] == ["no-new-privileges:true"]
    assert (worker["cpus"], worker["mem_limit"]) == (2.0, "2048m")
    # No source credential, no store password, no source folder.
    assert not {"REG_PASSWORD", "REG_USER", STORE_PASSWORD_ENV} & set(worker["environment"])
    # The API alone talks to it; the other services stay where they were.
    api = document["services"]["api"]
    assert api["networks"] == ["default", "compute", "plant-net"]
    assert api["environment"]["DTA_WORKER_URL"] == "http://worker:8100"
    assert "SOLVER_LICENCE" not in api["environment"]
    for other in ("store", "ui"):
        assert "networks" not in document["services"][other]
    assert (
        "DTA_WORKER_URL"
        not in yaml.safe_load(render_compose(plain))["services"]["api"]["environment"]
    )


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker command")
def test_compose_accepts_a_stack_with_a_worker(tmp_path: Path):
    plan = plan_stack(manifest(functions={"f_sim": CODE}, contract={"tier": "live"}))
    (tmp_path / "compose.yaml").write_text(render_compose(plan), encoding="utf-8")
    env_file = tmp_path / ".env"
    values = {**ENV, "SOLVER_LICENCE": "licence-0001"}

    def config(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        env_file.write_text("".join(f"{name}={value}\n" for name, value in env.items()))
        return subprocess.run(
            [*compose_command(plan, tmp_path, env_file), "config", "--quiet"],
            capture_output=True,
            text=True,
            timeout=120,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path)},
        )

    accepted = config(values)
    assert accepted.returncode == 0, accepted.stderr
    refused = config({k: v for k, v in values.items() if k != "SOLVER_LICENCE"})
    assert refused.returncode != 0 and "SOLVER_LICENCE" in refused.stderr


def test_a_twin_without_the_interface_has_no_ui_service():
    plan = plan_stack(manifest(interfaces={"ui": False}))
    document = yaml.safe_load(render_compose(plan))
    assert set(document["services"]) == {"store", "api"}
    assert "DTA_UI_PORT" not in plan.required_env()


def test_images_and_variable_names_come_from_the_manifest():
    plan = plan_stack(
        manifest(
            runtime={"image": "registry.example.org/dta-runtime:9", "store_image": "dolt:x"},
            interfaces={"token_env": "MY_TOKEN", "api_port_env": "MY_API", "ui_port_env": "MY_UI"},
        )
    )
    document = yaml.safe_load(render_compose(plan))
    assert document["services"]["api"]["image"] == "registry.example.org/dta-runtime:9"
    assert document["services"]["store"]["image"] == "dolt:x"
    assert document["services"]["api"]["environment"]["DTA_API_TOKEN"].startswith("${MY_TOKEN:?")
    assert document["services"]["ui"]["ports"] == ["127.0.0.1:${MY_UI:?MY_UI is required}:8050"]


def test_the_api_alone_joins_the_networks_of_sources_on_this_machine():
    plain = yaml.safe_load(render_compose(plan_stack(manifest())))
    assert "networks" not in plain and "networks" not in plain["services"]["api"]
    plan = plan_stack(manifest(runtime={"networks": ["plant_default", "plant_default", "lab"]}))
    assert plan.networks == ("plant_default", "lab")
    document = yaml.safe_load(render_compose(plan))
    assert document["networks"] == {
        "default": {},
        "plant_default": {"external": True},
        "lab": {"external": True},
    }
    assert document["services"]["api"]["networks"] == ["default", "plant_default", "lab"]
    assert "networks" not in document["services"]["store"]
    assert "networks" not in document["services"]["ui"]
    for name in ("default", "has space", ""):
        with pytest.raises(StackError):
            plan_stack(manifest(runtime={"networks": [name]}))


def test_the_same_manifest_always_gives_the_same_file():
    assert render_compose(plan_stack(manifest())) == render_compose(plan_stack(manifest()))


def _acting(*, kind: str = "webhook", executors: bool = True, **more: Any) -> Manifest:
    """A twin that notifies and whose action calls the plant."""
    notification: dict[str, Any] = {"kind": kind}
    if kind == "webhook":
        notification["webhook_env"] = "ALERT_URL"
    document: dict[str, Any] = {
        "contract": {
            "tier": "live",
            "notification": notification,
            "addons": {"executors": executors},
        },
        "actions": {
            "set_pump": {
                "binding": "executor",
                "executor": {
                    "kind": "http",
                    "url": "${PLANT_URL}/pumps/{id}",
                    "env": ["PLANT_URL", "PLANT_KEY"],
                    "headers": {"X-Key": "${PLANT_KEY}"},
                },
            },
            "advise_valve": {"binding": "advisory"},
        },
        **more,
    }
    return manifest(**document)


def test_the_api_alone_gets_what_its_notifications_and_executors_need():
    plan = plan_stack(_acting())
    assert plan.api_env == ("PLANT_URL", "PLANT_KEY", "ALERT_URL")
    assert set(plan.api_env) <= set(plan.required_env())
    document = yaml.safe_load(render_compose(plan))
    api = document["services"]["api"]
    for name in plan.api_env:
        assert api["environment"][name] == f"${{{name}:?{name} is required}}"
    for service in ("scheduler", "ui"):
        assert not set(plan.api_env) & set(document["services"][service]["environment"])
    # A log notification needs no address; without the add-on no call is made.
    assert plan_stack(_acting(kind="log")).api_env == ("PLANT_URL", "PLANT_KEY")
    assert plan_stack(_acting(kind="log", executors=False)).api_env == ()
    assert plan_stack(manifest()).api_env == ()


def test_the_api_is_labelled_with_what_it_reads_once_at_its_start():
    def label(document: Manifest) -> str:
        rendered = yaml.safe_load(render_compose(plan_stack(document)))
        return rendered["services"]["api"]["labels"]["dta.api-start"]

    first = label(_acting())
    assert first == plan_stack(_acting()).api_start and len(first) == 16
    # What the API reads only when it acts does not start it anew.
    calmer = _acting()
    calmer.actions["set_pump"].settle = timedelta(minutes=5)
    calmer.actions["set_pump"].executor.headers = {"X-Key": "${PLANT_KEY}", "X-Other": "1"}
    assert label(calmer) == first
    # Its notifier and its executors are made at its start.
    assert label(_acting(kind="log")) != first
    renamed = _acting()
    renamed.contract.notification.webhook_env = "OTHER_URL"
    assert label(renamed) != first
    assert label(_acting(executors=False)) != label(_acting(kind="webhook", executors=True))
    fewer = _acting()
    fewer.actions["set_pump"].executor.env = ["PLANT_URL"]
    assert label(fewer) != first


def test_a_relative_local_source_and_a_missing_twin_id_are_refused():
    with pytest.raises(StackError):
        plan_stack(
            manifest(sources={"local": {"connector": "file", "location": "file://data/files"}})
        )
    with pytest.raises(StackError):
        plan_stack(manifest(twin={"name": "No id"}))


ENV = {
    STORE_PASSWORD_ENV: "p" * 24,
    "DTA_API_TOKEN": "t" * 32,
    "DTA_API_PORT": "18700",
    "DTA_UI_PORT": "18701",
    "METER_GLOB": "meters/*.csv",
    "RECORDING_DIR": "/srv/recording",
    "REGION": "north",
    "REG_HOST": "db",
    "REG_PASSWORD": "r" * 24,
    "REG_USER": "reader",
}


def _config(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    plan = plan_stack(manifest())
    (tmp_path / "compose.yaml").write_text(render_compose(plan), encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{name}={value}\n" for name, value in env.items()))
    return subprocess.run(
        [*compose_command(plan, tmp_path, env_file), "config", "--quiet"],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path)},
    )


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker command")
def test_compose_accepts_the_file_and_refuses_to_start_without_a_variable(tmp_path: Path):
    accepted = _config(tmp_path, ENV)
    assert accepted.returncode == 0, accepted.stderr
    for missing in (STORE_PASSWORD_ENV, "DTA_API_TOKEN", "DTA_UI_PORT", "REG_PASSWORD"):
        refused = _config(tmp_path, {k: v for k, v in ENV.items() if k != missing})
        assert refused.returncode != 0
        assert missing in refused.stderr


def test_only_secret_values_are_masked(tmp_path) -> None:
    """A value declared as not secret is often an ordinary word and must stay readable."""
    from dta_toolkit.stack import Stack
    from dta_toolkit.workspace import Workspace

    workspace = Workspace(tmp_path)
    twin = manifest(
        env={
            "REG_HOST": {"description": "Host of the register", "secret": False},
            "REG_PASSWORD": {"description": "Password of the register"},
        }
    )
    workspace.env_path.write_text(
        "DTA_STORE_PASSWORD=store-secret-value\nDTA_API_TOKEN=token-secret-value\n"
        "DTA_API_PORT=18710\nDTA_BASE_URL=http://127.0.0.1:18710\n"
        "REG_HOST=registry\nREG_PASSWORD=register-secret\nREG_USER=reader-name\n",
        encoding="utf-8",
    )
    secret = Stack(workspace, twin).secret_values()
    assert set(secret) == {"DTA_STORE_PASSWORD", "DTA_API_TOKEN", "REG_PASSWORD", "REG_USER"}
