"""The toolkit commands that need no running stack: env, ontology check, skill, refusals."""

from __future__ import annotations

import stat
from pathlib import Path

import m3tk_kit as kit


def _env(twin: kit.KitTwin) -> dict[str, str]:
    lines = twin.env_file.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


# -- env -------------------------------------------------------------------------


def test_env_template_creates_a_private_env_and_an_example(make_twin) -> None:
    twin = make_twin()
    twin.write()
    result = twin.ok("env", "template")
    assert sorted(result["written"]) == [".env", ".env.example"]
    assert stat.S_IMODE(twin.env_file.stat().st_mode) == 0o600
    values = _env(twin)
    assert len(values["DTA_API_TOKEN"]) >= 32 and len(values["DTA_STORE_PASSWORD"]) >= 32
    assert values["DTA_API_PORT"] == "" and values[kit.DATA_ENV] == ""
    example = (twin.root / ".env.example").read_text(encoding="utf-8")
    for name in ("DTA_API_TOKEN", "DTA_STORE_PASSWORD", "DTA_API_PORT", kit.DATA_ENV):
        assert f"{name}=" in example
    assert "Folder holding the plant's CSV files" in example
    assert values["DTA_API_TOKEN"] not in example

    twin.fill_env()
    filled = _env(twin)
    again = twin.ok("env", "template")
    assert again == {"written": [], "generated": [], "to_fill": []}
    assert _env(twin) == filled  # nothing the user wrote is changed
    assert twin.leaked() == []


def test_env_template_adds_only_the_names_env_lacks(make_twin) -> None:
    twin = make_twin()
    twin.write()
    twin.env_file.write_text("DTA_API_TOKEN=chosen-by-the-user\n", encoding="utf-8")
    twin.env_file.chmod(0o600)
    result = twin.ok("env", "template")
    assert result["generated"] == ["DTA_STORE_PASSWORD"]
    assert _env(twin)["DTA_API_TOKEN"] == "chosen-by-the-user"
    assert stat.S_IMODE(twin.env_file.stat().st_mode) == 0o600


def test_env_check_names_each_missing_variable(make_twin) -> None:
    twin = make_twin()
    twin.write()
    error = twin.failed("env.missing", "env", "check")
    assert sorted(error["detail"]["missing"]) == sorted(
        ["DTA_STORE_PASSWORD", "DTA_API_TOKEN", "DTA_API_PORT", kit.DATA_ENV]
    )

    twin.ok("env", "template")
    error = twin.failed("env.missing", "env", "check")
    assert sorted(error["detail"]["missing"]) == ["DTA_API_PORT", kit.DATA_ENV]

    twin.fill_env()
    result = twin.ok("env", "check")
    assert result["missing"] == [] and result["warnings"] == []
    assert twin.leaked() == []


def test_env_check_warns_when_env_is_readable_by_others(make_twin) -> None:
    twin = make_twin()
    twin.write()
    twin.ok("env", "template")
    twin.fill_env()
    twin.env_file.chmod(0o644)
    result = twin.ok("env", "check")
    assert [w["code"] for w in result["warnings"]] == ["env.permissions"]


def test_probe_and_profile_refuse_to_start_without_every_variable(make_twin) -> None:
    twin = make_twin()
    twin.write()
    twin.ok("env", "template")
    for command in (("source", "probe"), ("profile",)):
        error = twin.failed("env.missing", *command)
        assert sorted(error["detail"]["missing"]) == ["DTA_API_PORT", kit.DATA_ENV]
    assert twin.gates()["access"] == "not_evaluated"
    assert not (twin.root / "compose.yaml").exists()  # nothing was started


# -- ontology check ----------------------------------------------------------------


def test_ontology_check_proposes_the_signature_and_detects_changed_types(make_twin) -> None:
    twin = make_twin()
    twin.write()
    error = twin.failed("ontology.signature", "ontology", "check")
    assert error["detail"]["manifest_signature"] is None

    written = twin.ok("ontology", "check", "--write")
    manifest = twin.dta.read_manifest()
    assert manifest["ontology"]["type_signature"] == written["file_signature"]
    assert manifest["field_status"]["ontology.type_signature"] == "proposed"
    assert twin.ok("ontology", "check")["agree"] is True

    twin.ok("manifest", "confirm", "--all-user")
    assert twin.dta.read_manifest()["field_status"]["ontology.type_signature"] == "confirmed"
    ontology = twin.root / "ontology" / "plant.yaml"
    ontology.write_text(ontology.read_text(encoding="utf-8") + kit.EXTRA_TYPE, encoding="utf-8")

    error = twin.failed("ontology.signature", "ontology", "check")
    assert error["detail"]["agree"] is False
    assert error["detail"]["manifest_signature"] == written["file_signature"]
    assert error["detail"]["file_signature"] != written["file_signature"]
    assert "Gauge" in {entry["name"] for entry in error["detail"]["types"]["objects"]}

    refused = twin.failed("build.not_ready", "build")
    assert "gaps" in {gate["name"] for gate in refused["detail"]["gates"]}


def test_ontology_check_reports_a_missing_or_broken_file(make_twin) -> None:
    twin = make_twin()
    twin.write()
    ontology = twin.root / "ontology" / "plant.yaml"
    ontology.write_text("classes: [not, a, mapping\n", encoding="utf-8")
    error = twin.failed("ontology.unusable", "ontology", "check")
    assert error["detail"]["path"] == "ontology/plant.yaml"

    ontology.unlink()
    twin.failed("ontology.missing", "ontology", "check", exit_code=2)

    data = twin.dta.read_manifest()
    del data["ontology"]
    twin.dta.write_manifest(data)
    twin.failed("ontology.no_path", "ontology", "check", exit_code=2)


# -- refusals before the stack exists -------------------------------------------------


def test_build_is_refused_until_gates_0_to_5_pass(make_twin) -> None:
    twin = make_twin()
    twin.write()
    error = twin.failed("build.not_ready", "build")
    unpassed = {gate["name"] for gate in error["detail"]["gates"]}
    assert {"contract", "access", "profiling", "design", "gaps"} <= unpassed
    assert not (twin.root / ".dta" / "build.json").exists()
    assert not (twin.root / "compose.yaml").exists()
    assert twin.gates()["build"] == "not_evaluated"


def test_build_rejects_an_unknown_step(make_twin) -> None:
    twin = make_twin()
    twin.write()
    error = twin.failed("build.bad_step", "build", "--from", "nowhere", exit_code=2)
    assert "load" in error["detail"]["steps"]


def test_accept_is_refused_before_a_build(make_twin) -> None:
    twin = make_twin()
    twin.write()
    twin.failed("accept.not_built", "accept")
    assert not (twin.root / "reports").exists()
    assert twin.gates()["acceptance"] == "not_evaluated"


# -- skill -------------------------------------------------------------------------


def test_generate_skill_brings_the_skill_directory_in_line(make_twin) -> None:
    twin = make_twin()
    twin.write()
    first = twin.ok("generate", "skill")
    directory = Path(first["path"])
    assert first["name"] == f"twin-{twin.id}" and directory.parent == twin.root / "skill"
    assert sorted(first["written"]) == sorted(first["files"])
    assert (directory / "SKILL.md").is_file() and (directory / "twin.json").is_file()

    stray = directory / "notes.md"
    stray.write_text("left over\n", encoding="utf-8")
    second = twin.ok("generate", "skill")
    assert second["written"] == [] and second["removed"] == [f"{first['name']}/notes.md"]
    assert not stray.exists()


def test_install_skill_installs_records_the_handover_and_replaces_a_copy(
    make_twin, tmp_path: Path, home: Path
) -> None:
    twin = make_twin()
    twin.write()
    twin.ok("generate", "skill")
    target = tmp_path / "skills"

    result = twin.ok("install-skill", "--target", str(target))
    destination = target / f"twin-{twin.id}"
    assert result["installed"] == str(destination)
    assert (destination / "SKILL.md").is_file()
    assert twin.dta.state()["installed_skill"] == str(destination)
    assert twin.gates()["handover"] == "passed"
    handover = (twin.root / "reports" / "handover.md").read_text(encoding="utf-8")
    assert "q_level" in handover and "DTA_API_TOKEN" in handover
    assert "Gate 2 (access) has not passed" in handover  # nothing was probed in this test

    (destination / "stale.md").write_text("from an earlier install\n", encoding="utf-8")
    twin.ok("install-skill", "--target", str(target))
    assert not (destination / "stale.md").exists()
    assert sorted(p.name for p in target.iterdir()) == [destination.name]  # no staging left
    assert not any(home.iterdir())


def test_install_skill_personal_scope_installs_under_home(make_twin, home: Path) -> None:
    twin = make_twin()
    twin.write()
    twin.ok("generate", "skill")
    result = twin.ok("install-skill", "--scope", "personal")
    assert result["installed"] == str(home / ".claude" / "skills" / f"twin-{twin.id}")


def test_install_skill_refuses_a_skill_from_another_manifest(make_twin, tmp_path: Path) -> None:
    twin = make_twin()
    twin.write()
    generated = twin.ok("generate", "skill")
    data = twin.dta.read_manifest()
    data["twin"]["name"] = "Plant levels (renamed)"
    twin.dta.write_manifest(data)

    target = tmp_path / "skills"
    error = twin.failed("skill.stale", "install-skill", "--target", str(target))
    assert error["detail"]["skill_manifest_hash"] == generated["manifest_hash"]
    assert error["detail"]["manifest_hash"] != generated["manifest_hash"]
    assert not target.exists()
    assert twin.gates()["handover"] == "not_evaluated"
    assert "installed_skill" not in twin.dta.state() or not twin.dta.state()["installed_skill"]


def test_install_skill_refuses_without_a_skill_or_with_two_targets(
    make_twin, tmp_path: Path
) -> None:
    twin = make_twin()
    twin.write()
    twin.failed("skill.missing", "install-skill", "--target", str(tmp_path / "skills"))
    twin.ok("generate", "skill")
    twin.failed(
        "install.options",
        "install-skill",
        "--target",
        str(tmp_path / "skills"),
        "--scope",
        "project",
        exit_code=2,
    )
    assert not (tmp_path / "skills").exists()
