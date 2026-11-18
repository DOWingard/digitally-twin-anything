"""A function of the manifest names something that can run: what the manifest's own checks see."""

from typing import Any

from dta_core.manifest import manifest_hash, parse_manifest
from dta_core.manifest.checks import run_checks


def _manifest(functions: dict[str, Any], env: dict[str, Any] | None = None) -> Any:
    document: dict[str, Any] = {"twin": {"id": "plant", "name": "Plant"}, "functions": functions}
    if env is not None:
        document["env"] = env
    return parse_manifest(document)


def _findings(manifest: Any, code: str = "ref.function_entry") -> list[tuple[str, str]]:
    return [(f.path, f.message) for f in run_checks(manifest) if f.code == code]


def _builtin(entry: str, **params: Any) -> dict[str, Any]:
    return {"kind": "builtin", "entry": entry, "params": params}


def test_built_ins_with_parameters_they_take_pass():
    functions = {
        "f_latest": _builtin("latest"),
        "f_below": _builtin("below", property="Junction.pressure", threshold=20, unit="m"),
        "f_energy": _builtin("integrate", property="Pump.power", period="P1D", unit="kW.h"),
        "f_paths": _builtin(
            "paths", edge_types=["Pipe", "Pump", "Valve"], edge_where=["Pipe.status:eq:open"]
        ),
        "f_code": {"kind": "registered", "entry": "plant.hydraulics:simulate"},
        # Nothing is judged before the kind and the entry are stated.
        "f_open": {"description": "not designed yet"},
    }
    assert _findings(_manifest(functions)) == []


def test_an_entry_that_is_no_built_in_is_named_with_the_built_ins():
    ((path, message),) = _findings(_manifest({"f": _builtin("newest")}))
    assert path == "functions.f.entry"
    assert "'newest' is not a built-in function" in message and "latest" in message


def test_a_parameter_the_built_in_does_not_take_and_one_of_the_wrong_shape_are_each_named():
    functions = {"f": _builtin("below", property="pressure", threshold="20", limit=3)}
    assert _findings(_manifest(functions)) == [
        ("functions.f.params.limit", _findings(_manifest(functions))[0][1]),
        ("functions.f.params.property", "property must be a property written Type.property"),
        ("functions.f.params.threshold", "threshold must be a finite number"),
    ]
    assert (
        "below takes no parameter 'limit'; it takes property, threshold"
        in (_findings(_manifest(functions))[0][1])
    )


def test_where_clauses_the_twin_cannot_read_are_named_as_it_would():
    """The water twin's first filters, written as SQL, passed validate as unknown properties."""
    where = ["mean_demand > 0", "mean_demand>0", "mean_demand:above:0", "status:null:x", "x:eq"]
    edge_where = ["status:eq:open", "Pipe.status:eq:open"]
    functions = {
        "f": _builtin("latest", property="Junction.mean_demand", where=where),
        "g": _builtin("paths", edge_where=edge_where),
    }
    form = "<property>:<op>:<value>"
    expected = [
        ("f", "where", f"a filter is written {form}, got 'mean_demand > 0'"),
        ("f", "where", f"a filter is written {form}, got 'mean_demand>0'"),
        (
            "f",
            "where",
            "unknown filter operator 'above' in 'mean_demand:above:0'; use one of eq, ne, lt, "
            "le, gt, ge, in, like, null, notnull",
        ),
        ("f", "where", "filter on status: null takes no value"),
        ("f", "where", "filter on x: eq needs a value"),
        ("g", "edge_where", f"an edge filter is written <LinkType>.{form}, got 'status:eq:open'"),
    ]
    assert sorted(_findings(_manifest(functions))) == sorted(
        (f"functions.{fid}.params.{name}", message) for fid, name, message in expected
    )


def test_code_is_named_as_module_and_function():
    ((path, message),) = _findings(_manifest({"f": {"kind": "registered", "entry": "simulate"}}))
    assert path == "functions.f.entry" and "module:function" in message


def test_a_variable_a_function_may_read_has_to_be_declared():
    function = {"kind": "registered", "entry": "plant:simulate", "env": ["PLANT_LICENCE"]}

    def of_the_function(manifest: Any, code: str) -> list[tuple[str, str]]:
        return [found for found in _findings(manifest, code) if "PLANT_LICENCE" in found[1]]

    assert of_the_function(_manifest({"f": function}), "ref.env_declared") == [
        ("functions.f.env", "variable 'PLANT_LICENCE' is used but not declared in env")
    ]
    declared = _manifest({"f": function}, env={"PLANT_LICENCE": {"description": "Solver licence"}})
    assert of_the_function(declared, "ref.env_declared") == []
    assert of_the_function(declared, "ref.env_unused") == []
    unused = _manifest({}, env={"PLANT_LICENCE": {"description": "Solver licence"}})
    assert [path for path, _ in of_the_function(unused, "ref.env_unused")] == ["env.PLANT_LICENCE"]


def test_fields_left_at_their_defaults_do_not_change_the_hash():
    """Writing out what the software would fill in anyway states nothing new."""
    plain = {"kind": "registered", "entry": "plant:simulate"}
    assert manifest_hash(_manifest({"f": plain})) == manifest_hash(
        _manifest({"f": {**plain, "env": []}})
    )
    assert manifest_hash(_manifest({"f": plain})) != manifest_hash(
        _manifest({"f": {**plain, "env": ["PLANT_LICENCE"]}})
    )
