"""Running code a twin brings: a module on the path, a reader of the real store, and
what each kind of thing it gives back becomes."""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pytest
from dta_runtime.functions import FunctionError, registered
from dta_runtime.protocol import HANDLERS, Artifacts, FunctionRequest, FunctionResult, Reader
from functions_testkit import History, KeptArtifacts, StoreReader, node

CODE = '''
"""Functions of the water twin, as its code artifact holds them."""

from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.compute as pc
from dta_core.schema import FactKind
from dta_runtime.protocol import Answer, Derived, Interval


def _tanks(twin):
    return twin.table("/objects/Tank", columns="level,max_level")


def mean_level(twin):
    return pc.mean(_tanks(twin).column("level")).as_py()


def describe(twin, uid, margin=0.5):
    tank = twin.get(f"/objects/Tank/{uid}")["properties"]
    return {
        "tank": tank["name"],
        "at": twin.at,
        "branch": twin.branch,
        "headroom": [tank["max_level"] - tank["level"], margin],
        "full": tank["level"] >= tank["max_level"] - margin,
        "note": None,
    }


def levels(twin, above=0.0):
    tanks = _tanks(twin)
    return tanks.filter(pc.greater(tanks.column("level"), above)).select(["uid", "level"])


def nothing(twin):
    return None


def mean_with_interval(twin):
    value = mean_level(twin)
    return Answer(
        value=value,
        unit="m",
        interval=Interval(low=value - 1.0, high=value + 1.0, level=95),
        notes=("outside_validity",),
    )


def levels_with_unit(twin):
    return Answer(value=levels(twin), unit="m", notes=("truncated",))


def headroom(twin, confidence=0.8):
    tanks = _tanks(twin)
    rows = pa.table(
        {
            "uid": tanks.column("uid"),
            "pressure": pc.subtract(tanks.column("max_level"), tanks.column("level")),
        }
    )
    return Derived(type="Junction", rows=rows, kind=FactKind.inferred, confidence=confidence)


def a_set(twin):
    return {1, 2, 3}


def not_a_number(twin):
    return {"levels": [1.0, float("nan")]}


def a_time(twin):
    return {"read": {"when": datetime(2026, 1, 7, tzinfo=UTC)}}


def a_time_as_text(twin):
    return {"read": {"when": datetime(2026, 1, 7, tzinfo=UTC).isoformat()}}


def a_key_that_is_no_text(twin):
    return [{"levels": {1: 2.0}}]


def an_object(twin):
    return object()


def an_interval_that_is_none(twin):
    return Answer(value=1.0, interval=(0.0, 2.0))


def an_endless_interval(twin):
    return Answer(value=1.0, interval=Interval(low=0.0, high=float("inf"), level=95))


def a_unit_that_is_no_text(twin):
    return Answer(value=1.0, unit=5)


def observed_rows(twin):
    return Derived(type="Junction", rows=pa.table({"uid": ["node:J1"]}), kind=FactKind.observed)


def rows_without_uid(twin):
    return Derived(type="Junction", rows=pa.table({"pressure": [1.0]}), kind="inferred")


def rows_that_are_no_table(twin):
    return Derived(type="Junction", rows=[{"uid": "node:J1"}], kind="inferred")


def too_sure(twin):
    return Derived(
        type="Junction", rows=pa.table({"uid": ["node:J1"]}), kind="assumed", confidence=1.5
    )


def fails(twin):
    return 1 / 0


not_a_function = 3
'''


@pytest.fixture
def module(tmp_path: Path, monkeypatch) -> str:
    """The twin's code where the worker puts it: unpacked, first on the path."""
    name = f"water_code_{uuid.uuid4().hex[:8]}"
    package = tmp_path / "code" / name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "checks.py").write_text(CODE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "code"))
    yield f"{name}.checks"
    for loaded in [found for found in sys.modules if found.split(".")[0] == name]:
        del sys.modules[loaded]


@pytest.fixture
def twin(story: History) -> StoreReader:
    return StoreReader(story.twin, story.head)


@pytest.fixture
def artifacts(tmp_path: Path) -> KeptArtifacts:
    return KeptArtifacts(tmp_path / "artifacts")


def _request(module: str, name: str, twin: StoreReader, **args: Any) -> FunctionRequest:
    return FunctionRequest(
        artifact="ab" * 32,
        entry=f"{module}:{name}",
        args=args,
        at=twin.at,
        branch=twin.branch,
        budget_s=30,
        trace_id="0af7651916cd43dd8448eb211c80319c",
    )


def _run(module: str, name: str, twin: StoreReader, artifacts: KeptArtifacts, **args: Any) -> Any:
    return registered.run(_request(module, name, twin, **args), twin, artifacts)


def _refused(module: str, name: str, twin: StoreReader, artifacts: KeptArtifacts) -> FunctionError:
    with pytest.raises(FunctionError) as raised:
        _run(module, name, twin, artifacts)
    return raised.value


def test_it_is_the_handler_the_worker_is_told_of(twin, artifacts):
    assert HANDLERS["function"] == "dta_runtime.functions.registered:run"
    assert isinstance(twin, Reader) and isinstance(artifacts, Artifacts)


def test_a_bare_number_is_an_answer_without_unit_interval_or_notes(module, twin, artifacts):
    result = _run(module, "mean_level", twin, artifacts)
    assert isinstance(result, FunctionResult)
    # T1 7.0, T2 1.9 and T4 1.0, read from the store at the head.
    assert result.value == pytest.approx(3.3)
    assert (result.table, result.unit, result.interval, result.notes) == (False, None, None, [])
    assert result.derived is None
    # Left for the worker, which measures the run and collects what it logged.
    assert result.duration_ms == 0.0 and result.logs == []


def test_a_bare_document_is_given_its_arguments(module, twin, artifacts, story: History):
    result = _run(module, "describe", twin, artifacts, uid=node("T1"), margin=13.5)
    assert result.value == {
        "tank": "T1",
        "at": story.head.id,
        "branch": "main",
        "headroom": [13.0, 13.5],
        "full": True,
        "note": None,
    }
    assert _run(module, "describe", twin, artifacts, uid=node("T1")).value["full"] is False
    assert _run(module, "nothing", twin, artifacts).value is None


def test_a_bare_table_is_given_back_beside_its_result(module, twin, artifacts):
    table, result = _run(module, "levels", twin, artifacts, above=1.5)
    assert table.to_pylist() == [
        {"uid": node("T1"), "level": 7.0},
        {"uid": node("T2"), "level": 1.9},
    ]
    assert result.table is True and result.value is None
    assert (result.unit, result.interval, result.notes) == (None, None, [])


def test_an_answer_keeps_its_unit_interval_and_notes(module, twin, artifacts):
    result = _run(module, "mean_with_interval", twin, artifacts)
    assert result.value == pytest.approx(3.3)
    assert result.unit == "m"
    assert result.interval == pytest.approx({"low": 2.3, "high": 4.3, "level": 95.0})
    assert result.notes == ["outside_validity"]
    table, with_table = _run(module, "levels_with_unit", twin, artifacts)
    assert table.num_rows == 3
    assert (with_table.table, with_table.unit, with_table.notes) == (True, "m", ["truncated"])


def test_derived_rows_are_stored_and_read_back_as_the_same_table(module, twin, artifacts):
    result = _run(module, "headroom", twin, artifacts, confidence=0.75)
    assert isinstance(result, FunctionResult)
    assert (result.value, result.table) == (None, False)
    assert result.derived_type == "Junction"
    assert result.derived_kind == "inferred"
    assert result.derived_confidence == 0.75
    assert result.derived is not None
    assert artifacts.media_types == {result.derived: "application/vnd.apache.arrow.file"}
    stored = pa.ipc.open_file(artifacts.get(result.derived)).read_all()
    # The table the function made, from the same read of the store.
    tanks = twin.table("/objects/Tank", columns="level,max_level")
    made = pa.table(
        {
            "uid": tanks.column("uid"),
            "pressure": pc.subtract(tanks.column("max_level"), tanks.column("level")),
        }
    )
    assert stored.equals(made)
    assert stored.column("uid").to_pylist() == [node(key) for key in ("T1", "T2", "T4", "T5")]
    # 20 m less each level; T5 has none.
    assert stored.column("pressure").to_pylist()[:3] == pytest.approx([13.0, 18.1, 19.0])
    assert stored.column("pressure").to_pylist()[3] is None
    # The same rows are the same file.
    again = _run(module, "headroom", twin, artifacts, confidence=0.75)
    assert again.derived == result.derived


@pytest.mark.parametrize(
    ("name", "where", "said"),
    [
        ("a_set", "$", "set"),
        ("not_a_number", "$.levels[1]", "nan"),
        ("a_time", "$.read.when", "datetime"),
        ("a_key_that_is_no_text", "$[0].levels", "keys"),
        ("an_object", "$", "object"),
        ("an_interval_that_is_none", "interval", "Interval"),
        ("an_endless_interval", "interval.high", "inf"),
        ("a_unit_that_is_no_text", "unit", "string"),
        ("observed_rows", "kind", "observed"),
        ("rows_without_uid", "rows", "uid"),
        ("rows_that_are_no_table", "rows", "table"),
        ("too_sure", "confidence", "1.5"),
    ],
)
def test_what_cannot_be_an_answer_is_refused_and_where_is_named(
    module, twin, artifacts, name, where, said
):
    error = _refused(module, name, twin, artifacts)
    assert error.code == "function.bad_result"
    assert error.detail["function"] == f"{module}:{name}"
    assert error.detail["where"] == where
    assert where in error.message and said in error.message
    # Nothing is stored for a result that is refused.
    assert artifacts.media_types == {}


def test_a_time_written_as_text_is_json(module, twin, artifacts):
    result = _run(module, "a_time_as_text", twin, artifacts)
    assert result.value == {"read": {"when": "2026-01-07T00:00:00+00:00"}}


def test_an_entry_that_names_nothing_is_refused(module, twin, artifacts):
    for entry in ("missing", "not_a_function"):
        error = _refused(module, entry, twin, artifacts)
        assert error.code == "function.bad_entry"
        assert entry in error.message
    package = module.split(".")[0]
    for absent in (f"{package}.hydraulics", "no_such_code_at_all.checks"):
        error = _refused(absent, "mean_level", twin, artifacts)
        assert error.code == "function.bad_entry"
        assert error.detail["function"] == f"{absent}:mean_level"


def test_arguments_the_function_does_not_take_are_refused(module, twin, artifacts):
    for args in ({"margin": 1.0}, {"uid": node("T1"), "depth": 2}):
        with pytest.raises(FunctionError) as raised:
            _run(module, "describe", twin, artifacts, **args)
        assert raised.value.code == "function.bad_param"
        assert raised.value.detail["function"] == f"{module}:describe"


def test_what_the_function_raises_is_raised(module, twin, artifacts):
    with pytest.raises(ZeroDivisionError):
        _run(module, "fails", twin, artifacts)
    with pytest.raises(LookupError):
        _run(module, "describe", twin, artifacts, uid=node("T3"))


def test_the_handler_is_imported_without_the_store_or_the_graph():
    # The worker imports it in a fresh process for every run of a twin's own code.
    code = (
        "import sys, dta_runtime.functions.registered\n"
        "heavy = ('dta_core.store', 'duckdb', 'rustworkx', 'sqlalchemy', 'pint')\n"
        "print(sorted(name for name in heavy if name in sys.modules))\n"
        "from dta_runtime.functions import Context, FunctionError, resolve, run\n"
        "print(run.__module__, resolve.__module__, Context.__module__)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.splitlines() == [
        "[]",
        "dta_runtime.functions.runner dta_runtime.functions.params dta_runtime.functions.context",
    ]
