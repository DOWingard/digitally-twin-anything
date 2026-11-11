"""`dta manifest summary`."""

ONTOLOGY = """\
id: https://example.org/pump-station
name: pump_station
prefixes:
  linkml: https://w3id.org/linkml/
default_range: string
imports:
  - linkml:types
classes:
  Tank:
    attributes:
      level: {range: float}
  Pump:
    attributes:
      speed: {range: float}
  Feeds:
    annotations:
      link_source: pump
      link_target: tank
    attributes:
      pump: {range: Pump}
      tank: {range: Tank}
"""


def test_summary_is_byte_identical_across_runs(workspace, complete):
    workspace.write_manifest(complete)
    workspace.root.joinpath("ontology", "domain.yaml").write_text(ONTOLOGY)
    target = workspace.root / ".dta" / "summary.md"

    first = workspace.run("manifest", "summary")
    assert first.exit_code == 0, first.output
    first_file = target.read_bytes()
    second = workspace.run("manifest", "summary")
    assert second.exit_code == 0
    assert second.stdout.encode() == first.stdout.encode()
    assert target.read_bytes() == first_file

    text = first_file.decode()
    assert "| Object types | Pump, Tank |" in text
    assert "| Link types | Feeds |" in text
    assert "| q_forecast | What will each tank level be in six hours? | predictive |" in text
    assert "| c_pump_speed | Pump.speed | 0 to 1500; step <= 100 | supervised |" in text
    assert "Sync interval: 5 min" in text


def test_summary_json_and_out_option(workspace, complete):
    workspace.write_manifest(complete)
    code, document = workspace.json("manifest", "summary", "--out", "review/summary.md")
    assert code == 0
    result = document["result"]
    written = workspace.root / "review" / "summary.md"
    assert result["path"] == str(written)
    assert written.read_text() == result["markdown"]
    assert "Ontology file `ontology/domain.yaml` is not written yet." in result["markdown"]


def test_summary_refuses_to_write_outside_the_workspace(workspace, complete, tmp_path):
    workspace.write_manifest(complete)
    code, document = workspace.json("manifest", "summary", "--out", str(tmp_path / "out.md"))
    assert code == 2
    assert document["error"]["code"] == "workspace.outside"
    assert not (tmp_path / "out.md").exists()


def test_unreadable_ontology_cannot_be_summarised(workspace, complete):
    workspace.write_manifest(complete)
    workspace.root.joinpath("ontology", "domain.yaml").write_text("classes: [\n")
    code, document = workspace.json("manifest", "summary")
    assert code == 2
    assert document["error"]["code"] == "ontology.unreadable"
