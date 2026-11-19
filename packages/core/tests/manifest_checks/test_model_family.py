"""A candidate names a family that can run, and the twin's own code comes from somewhere."""

from typing import Any

from dta_core.families import BASELINES, FAMILIES, ORDER
from dta_core.manifest import parse_manifest
from dta_core.manifest.checks import run_checks


def _manifest(**sections: Any) -> Any:
    return parse_manifest({"twin": {"id": "plant", "name": "Plant"}, **sections})


def _findings(manifest: Any, code: str) -> list[tuple[str, str]]:
    return [(f.path, f.message) for f in run_checks(manifest) if f.code == code]


def _model(**candidates: str) -> dict[str, Any]:
    return {"m": {"candidates": {cid: {"family": family} for cid, family in candidates.items()}}}


def test_the_shipped_families_from_the_simplest_on():
    assert ORDER == (
        "persistence",
        "seasonal_naive",
        "theta",
        "ets",
        "arima",
        "mstl",
        "ridge_lags",
        "gbm_lags",
    )
    assert [name for name in ORDER if FAMILIES[name].baseline] == ["persistence", "seasonal_naive"]
    # What a model spec may name as the naive model to beat is one of them.
    assert set(BASELINES.values()) == {name for name in ORDER if FAMILIES[name].baseline}


def test_a_shipped_family_and_code_pass_and_anything_else_is_named():
    fine = _manifest(
        models=_model(a="theta", b="plant.hydraulics:Network"), runtime={"code": "code"}
    )
    assert _findings(fine, "ref.model_family") == []
    assert _findings(fine, "ref.code_declared") == []
    ((path, message),) = _findings(_manifest(models=_model(a="prophet")), "ref.model_family")
    assert path == "models.m.candidates.a.family"
    assert "'prophet' is neither a shipped family" in message and "module:name" in message


def test_code_that_declares_no_reads_is_warned_of_and_nothing_else_is():
    def spec(reads: list[str] | None = None, **candidates: str) -> dict[str, Any]:
        found = _model(**candidates)
        if reads is not None:
            found["m"]["reads"] = reads
        return found

    code = "plant.hydraulics:Network"
    undeclared = _manifest(models=spec(a="theta", b=code, c=code), runtime={"code": "code"})
    ((path, message),) = _findings(undeclared, "model.reads_undeclared")
    assert path == "models.m.reads"
    assert message.startswith("code of the twin's own (b, c) with no reads:")
    assert "read every property" in message and "none of its origins is scored" in message
    finding = next(f for f in run_checks(undeclared) if f.code == "model.reads_undeclared")
    assert finding.severity.value == "warning"
    # A name that is neither a family nor code is reported by ref.model_family alone.
    quiet = (spec([], b=code), spec(["Tank.level"], b=code), spec(None, a="theta", p="prophet"))
    for fine in quiet:
        assert (
            _findings(_manifest(models=fine, runtime={"code": "code"}), "model.reads_undeclared")
            == []
        )


def test_what_a_model_reads_is_a_property_reference_as_a_feature_is():
    models = {"m": {"candidates": {"a": {"family": "theta"}}, "reads": ["Tank.level", "level"]}}
    found = _findings(_manifest(models=models), "ref.property_format")
    assert [path for path, _ in found] == ["models.m.reads"]
    models["m"]["features"] = ["level"]
    found = _findings(_manifest(models=models), "ref.property_format")
    assert sorted(path for path, _ in found) == ["models.m.features", "models.m.reads"]


def test_code_of_the_twin_needs_its_directory():
    functions = {"f": {"kind": "registered", "entry": "plant:simulate"}}
    models = _model(a="theta", b="plant.hydraulics:Network")
    found = _findings(_manifest(functions=functions, models=models), "ref.code_declared")
    assert [path for path, _ in found] == ["functions.f.kind", "models.m.candidates.b.family"]
    assert all("runtime.code names no directory" in message for _, message in found)
    # A twin with shipped families and built-ins only has no code to declare.
    plain = _manifest(
        functions={"f": {"kind": "builtin", "entry": "latest"}}, models=_model(a="ets")
    )
    assert _findings(plain, "ref.code_declared") == []
