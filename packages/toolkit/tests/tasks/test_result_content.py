"""What a result file may carry.

A result is written by another agent. Whatever of it is accepted ends up in the
manifest or in the task state, and every later command has to read both. So a
result is refused when storing it would leave either file unreadable, or would
leave a merge impossible to repeat after an interruption.
"""

import json

import pytest
from dta_toolkit.tasks.result import MAX_DEPTH, MAX_RESULT_BYTES

# Half a character: what a file name that is not text turns into once a program lists it.
LONE = "\udcff"


def _nested(levels: int) -> str:
    return "[" * levels + "]" * levels


def _with_extra(extra: str) -> str:
    """A result whose single proposal carries `extra`, given as JSON text.

    The value sits three levels below the top of the document.
    """
    return (
        '{"manifest_patch": {}, "proposals": [{"target": "ontology", "description": "x", '
        f'"extra": {extra}}}], "artifacts": [], "notes": ""}}'
    )


def _claimed(ws, task="profile-erp"):
    ws.open("3")
    return ws.claim(task)


def _result_file(ws, task="profile-erp"):
    return ws.output(task) / "result.json"


# -- nesting -------------------------------------------------------------------


@pytest.mark.parametrize("levels", [MAX_DEPTH - 2, 200, 300, 100_000])
def test_a_result_nested_too_deeply_never_reaches_the_state_file(ws, levels):
    token = _claimed(ws)
    state = ws.state_bytes()
    _result_file(ws).write_text(_with_extra(_nested(levels)))
    assert ws.submit("profile-erp", token).reasons == ["tasks.result.invalid"]
    assert ws.state_bytes() == state
    assert ws.status()["open_batch"]["id"] == "b1"  # the state is still readable


def test_the_deepest_result_that_is_accepted_comes_back_out_of_the_state_file(ws):
    token = _claimed(ws)
    _result_file(ws).write_text(_with_extra(_nested(MAX_DEPTH - 3)))
    assert ws.submit("profile-erp", token).code == 0
    ws.merge("profile-erp")
    kept = ws.status()["proposals"][0]["proposal"]["extra"]
    assert json.dumps(kept) == _nested(MAX_DEPTH - 3)
    assert ws.clean()


def test_a_patch_value_nested_too_deeply_never_reaches_the_manifest(make_ws, sample):
    ws = make_ws("deep", sample)
    ws.open("4")
    token = ws.claim("question-q1")
    before = ws.manifest_bytes()
    _result_file(ws, "question-q1").write_text(
        '{"manifest_patch": {"functions.q1": {"kind": "builtin", "entry": "latest", '
        f'"params": {{"x": {_nested(300)}}}}}}}, "proposals": [], "artifacts": [], "notes": ""}}'
    )
    assert ws.submit("question-q1", token).reasons == ["tasks.result.invalid"]
    assert ws.manifest_bytes() == before


# -- text that is not text -----------------------------------------------------


@pytest.mark.parametrize("where", ["patch key", "patch value", "proposal", "notes", "artifact"])
def test_text_that_cannot_be_written_to_a_file_is_refused_wherever_it_appears(ws, where):
    token = _claimed(ws)
    patch = ws.profile_patch("erp")
    document = {"manifest_patch": patch, "proposals": [], "artifacts": [], "notes": ""}
    datasets = patch["sources.erp.profile"]["datasets"]
    if where == "patch key":
        patch["sources.erp.profile"]["datasets"] = {f"caf{LONE}": datasets["main"]}
    elif where == "patch value":
        datasets["main"]["columns"]["id"]["dtype"] = f"caf{LONE}"
    elif where == "proposal":
        document["proposals"] = [{"target": "ontology", "description": "x", "file": LONE}]
    elif where == "notes":
        document["notes"] = f"profiled caf{LONE}.csv"
    else:
        document["artifacts"] = [f"caf{LONE}.csv"]
    text = json.dumps(document)
    # As an escape it is valid JSON; only decoding it reveals the half character.
    assert text.isascii()
    _result_file(ws).write_text(text)
    before = ws.manifest_bytes(), ws.state_bytes()

    assert ws.submit("profile-erp", token).reasons == ["tasks.result.invalid"]
    assert (ws.manifest_bytes(), ws.state_bytes()) == before


# -- numbers that are not numbers ----------------------------------------------


@pytest.mark.parametrize("number", ["1e999", "-1e999", "NaN", "Infinity"])
def test_a_number_out_of_range_is_refused_anywhere_in_a_result(ws, number):
    token = _claimed(ws)
    _result_file(ws).write_text(_with_extra(number))
    assert ws.submit("profile-erp", token).reasons == ["tasks.result.invalid"]


@pytest.mark.parametrize("number", ["18446744073709551616", "9" * 4000, "-0.0", "1e-320"])
def test_an_unusual_but_real_number_is_kept_as_it_was_given(ws, number):
    token = _claimed(ws)
    _result_file(ws).write_text(_with_extra(number))
    assert ws.submit("profile-erp", token).code == 0
    ws.merge("profile-erp")
    kept = ws.status()["proposals"][0]["proposal"]["extra"]
    assert kept == json.loads(number) and type(kept) is type(json.loads(number))


def test_a_patch_that_reads_as_a_non_number_is_refused(ws):
    token = _claimed(ws)
    patch = ws.profile_patch("erp")
    # Text in the file, but the manifest's schema reads it into a number field.
    patch["sources.erp.profile"]["datasets"]["main"]["columns"]["id"]["minimum"] = "nan"
    ws.write_result("profile-erp", patch)
    outcome = ws.submit("profile-erp", token)
    assert outcome.reasons == ["tasks.patch.unstable"]
    assert outcome.reason("tasks.patch.unstable")["problems"] == [
        {
            "path": "sources.erp.profile.datasets.main.columns.id.minimum",
            "problem": "holds a value that is not a number",
        }
    ]


# -- patches that do not survive the manifest file -----------------------------


@pytest.mark.parametrize(
    "text",
    [
        "throughput\x85now",
        "line separator",
        "carriage\rreturn",
        "both\r\nends",
        "﻿marked",
        " leading",
        "trailing ",
        "tab\there",
        "nul\x00inside",
        "#not a comment",
        "null",
        "0123",
        "2026-01-01",
        "multi\nline\n",
    ],
)
def test_an_accepted_patch_can_always_be_merged_again_after_an_interruption(make_ws, sample, text):
    ws = make_ws("text", sample)
    ws.open("4")
    token = ws.claim("question-q1")
    ws.write_result(
        "question-q1",
        {"functions.q1": {"kind": "builtin", "entry": "latest", "description": text}},
    )
    outcome = ws.submit("question-q1", token)
    if outcome.code != 0:
        # Refusing is the other acceptable answer, and it has to say why.
        assert outcome.reasons == ["tasks.patch.unstable"]
        assert ws.manifest().functions is None or "q1" not in ws.manifest().functions
        return
    ws.crash("merge.manifest_written", "merge", "question-q1")
    assert ws.task("question-q1")["status"] == "submitted"
    assert ws.merge("question-q1")["merged"] == ["question-q1"]
    assert ws.manifest().functions["q1"].description == text
    assert ws.clean()


def test_a_manifest_value_elsewhere_that_would_change_on_rewrite_holds_up_the_merge(ws):
    # Written by hand between batches: read back, this name holds a character
    # that the manifest writer does not preserve.
    path = ws.workspace.manifest_path
    path.write_text(path.read_text().replace("name: Plant", 'name: "Plant\\x85Floor"'))
    assert ws.manifest().twin.name == "Plant\x85Floor"
    token = _claimed(ws)
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    before = ws.manifest_bytes()

    outcome = ws.submit("profile-erp", token)
    assert outcome.reasons == ["tasks.manifest.unstable"]
    assert outcome.reason("tasks.manifest.unstable")["problems"] == [
        {"path": "twin.name", "problem": "does not read back as written"}
    ]
    assert ws.manifest_bytes() == before


# -- patches that would break later planning -----------------------------------


def test_a_patch_that_would_leave_a_later_stage_impossible_to_plan_is_refused(make_ws, sample):
    ws = make_ws("ids", sample)
    ws.open("4")
    token = ws.claim("question-q2")
    patch = {
        "models.q2": {"question": "q2", "candidates": {"ARIMA.v2": {"family": "arima"}}},
        "contract.questions.q2.model": "q2",
    }
    ws.write_result("question-q2", patch)
    outcome = ws.submit("question-q2", token)
    assert outcome.reasons == ["tasks.patch.unplannable"]
    assert list(outcome.reason("tasks.patch.unplannable")["stages"]) == ["6"]

    patch["models.q2"]["candidates"] = {"arima-v2": {"family": "arima"}}
    ws.write_result("question-q2", patch)
    assert ws.submit("question-q2", token).code == 0
    ws.merge("question-q2")
    planned = ws.run("plan", "--stage", "6").result
    assert "model-q2-arima-v2" in [task["id"] for task in planned["tasks"]]


# -- size ----------------------------------------------------------------------


def test_a_result_file_too_large_to_keep_in_the_state_is_refused(ws):
    token = _claimed(ws)
    ws.write_result("profile-erp", ws.profile_patch("erp"), notes="x" * MAX_RESULT_BYTES)
    outcome = ws.submit("profile-erp", token)
    assert outcome.reasons == ["tasks.result.invalid"]
    assert outcome.reason("tasks.result.invalid")["limit"] == MAX_RESULT_BYTES


# -- reporting -----------------------------------------------------------------


def test_a_refused_submit_reports_what_is_wrong_with_the_patch_along_with_the_rest(ws):
    token = _claimed(ws)
    patch = ws.profile_patch("erp")
    patch["sources.erp.profile"]["datasets"]["main"]["rows"] = -5
    ws.write_result("profile-erp", patch, files={"a.txt": "a"}, artifacts=[])
    assert ws.submit("profile-erp", token).reasons == [
        "tasks.artifact.undeclared",
        "tasks.patch.schema",
    ]
    # Keys outside the task's own are named once, not once more as a side effect.
    ws.write_result("profile-erp", {"twin.id": "hijacked"}, files={"a.txt": "a"})
    assert ws.submit("profile-erp", token).reasons == ["tasks.patch.not_owned"]


def test_a_proposal_cannot_pass_for_another_tasks(ws):
    ws.open("3")
    forged = {
        "target": "ontology",
        "description": "drop class Machine",
        "task": "profile-sensors",
        "batch": "b7",
    }
    ws.finish("profile-erp", ws.profile_patch("erp"), proposals=[forged])
    ws.merge("profile-erp")
    assert ws.status()["proposals"] == [{"task": "profile-erp", "batch": "b1", "proposal": forged}]


# -- command lines that are not text -------------------------------------------


def test_arguments_that_are_not_text_are_answered_like_any_other_wrong_argument(ws):
    token = _claimed(ws)
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    refused = ws.spawn_raw(b"submit", b"profile-erp", b"--token", b"\xff\xfe")
    assert refused.reasons == ["tasks.lease.token"]
    ws.spawn_raw(
        b"claim", b"profile-erp", b"--holder", b"agent-1", b"--renew", b"--token", b"\xff"
    ).refused("tasks.lease.token")
    ws.spawn_raw(b"claim", b"profile-sensors", b"--holder", b"a\xff").could_not_run(
        "tasks.holder.invalid"
    )
    ws.spawn_raw(b"claim", b"profile-\xff", b"--holder", b"a").could_not_run("tasks.id.invalid")
    assert ws.submit("profile-erp", token).code == 0
