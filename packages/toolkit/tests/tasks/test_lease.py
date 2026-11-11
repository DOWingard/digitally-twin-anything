import hashlib
import time
from datetime import timedelta

import pytest
from dta_toolkit.tasks import lifecycle

RACERS = 8
ROUNDS = 12


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def clock(monkeypatch):
    """Lets a test move the lease clock forward instead of waiting."""
    start = lifecycle._now()
    offset = [timedelta()]
    monkeypatch.setattr(lifecycle, "_now", lambda: start + offset[0])

    def advance(seconds):
        offset[0] += timedelta(seconds=seconds)

    return advance


# -- one lease per task, under contention --------------------------------------


def test_processes_racing_for_one_task_get_exactly_one_lease(make_ws):
    for round_number in range(ROUNDS):
        ws = make_ws(f"race-{round_number}")
        ws.open("3")
        outcomes = ws.race(
            *[("claim", "profile-erp", "--holder", f"agent-{n}") for n in range(RACERS)]
        )
        winners = [outcome for outcome in outcomes if outcome.code == 0]
        losers = [outcome for outcome in outcomes if outcome.code != 0]
        assert len(winners) == 1, [outcome.payload for outcome in outcomes]
        assert len(losers) == RACERS - 1
        won = winners[0].result
        for loser in losers:
            detail = loser.refused("tasks.lease.held")
            assert detail["lease"]["holder"] == won["holder"]
            assert "token" not in str(loser.payload)

        # The state on disk agrees with what the winner was told, and nobody else left a mark.
        record = ws.task("profile-erp")
        assert record["status"] == "claimed"
        assert record["lease"]["holder"] == won["holder"]
        assert record["lease"]["generation"] == 1
        assert record["lease"]["token_sha256"] == _digest(won["token"])
        assert won["token"] not in ws.workspace.tasks_state_path.read_text()
        assert ws.task("profile-sensors")["status"] == "open"
        assert ws.clean()

        # The winner's token is the one that works.
        ws.write_result("profile-erp", ws.profile_patch("erp"))
        assert ws.submit("profile-erp", won["token"]).code == 0


def test_claims_of_different_tasks_at_once_are_all_kept(make_ws, sample):
    sample["sources"] = {
        f"s{n}": {"connector": "file", "location": f"{n}.csv"} for n in range(RACERS)
    }
    for round_number in range(4):
        ws = make_ws(f"spread-{round_number}", sample)
        ws.open("3")
        outcomes = ws.race(
            *[("claim", f"profile-s{n}", "--holder", f"agent-{n}") for n in range(RACERS)]
        )
        # Every claim reads, changes and rewrites the same state file. None may be lost.
        for n, outcome in enumerate(outcomes):
            record = ws.task(f"profile-s{n}")
            assert record["lease"]["holder"] == f"agent-{n}"
            assert record["lease"]["token_sha256"] == _digest(outcome.result["token"])
        assert ws.clean()


def test_a_claim_and_a_takeover_attempt_racing_leave_one_holder(make_ws):
    for round_number in range(4):
        ws = make_ws(f"mixed-{round_number}")
        ws.open("3")
        first = ws.claim("profile-erp", holder="first", ttl=1)
        time.sleep(1.2)
        outcomes = ws.race(
            *[("claim", "profile-erp", "--holder", f"agent-{n}") for n in range(4)],
            ("claim", "profile-erp", "--holder", "first", "--renew", "--token", first),
        )
        takeovers = [outcome for outcome in outcomes[:4] if outcome.code == 0]
        renewal = outcomes[4]
        record = ws.task("profile-erp")
        # Either the old holder renewed first and keeps the task, or exactly one
        # newcomer took it over and the old token is dead. Never both.
        if record["lease"]["holder"] == "first":
            assert renewal.code == 0 and takeovers == []
            assert record["lease"]["token_sha256"] == _digest(first)
        else:
            assert len(takeovers) == 1
            assert record["lease"]["token_sha256"] == _digest(takeovers[0].result["token"])
            assert record["lease"]["generation"] == 2
            if renewal.code != 0:
                renewal.refused("tasks.lease.token")
            ws.write_result("profile-erp", ws.profile_patch("erp"))
            assert ws.submit("profile-erp", first).reasons == ["tasks.lease.token"]


# -- expiry, takeover, stale tokens --------------------------------------------


def test_an_expired_lease_can_be_taken_over_and_the_old_token_is_refused(ws, clock):
    ws.open("3")
    old = ws.claim("profile-erp", holder="alice", ttl=60)
    (ws.output("profile-erp") / "half-done.txt").write_text("partial")

    clock(59)
    held = ws.run("claim", "profile-erp", "--holder", "bob").refused("tasks.lease.held")
    assert held["lease"]["holder"] == "alice" and held["lease"]["expired"] is False

    clock(2)
    taken = ws.run("claim", "profile-erp", "--holder", "bob").result
    assert taken["taken_over_from"] == "alice" and taken["generation"] == 2
    assert taken["token"] != old
    assert list(ws.output("profile-erp").iterdir()) == []  # bob starts from nothing

    ws.write_result("profile-erp", ws.profile_patch("erp"))
    state = ws.state_bytes()
    stale = ws.submit("profile-erp", old)
    assert stale.reasons == ["tasks.lease.token"]
    assert stale.reason("tasks.lease.token")["lease"]["holder"] == "bob"
    ws.run("claim", "profile-erp", "--holder", "alice", "--renew", "--token", old).refused(
        "tasks.lease.token"
    )
    assert ws.state_bytes() == state

    assert ws.submit("profile-erp", taken["token"]).code == 0
    assert ws.task("profile-erp")["lease"]["holder"] == "bob"


def test_takeover_works_across_real_processes_and_real_time(ws):
    ws.open("3")
    old = ws.spawn("claim", "profile-erp", "--holder", "alice", "--ttl", "1").result["token"]
    ws.spawn("claim", "profile-erp", "--holder", "bob").refused("tasks.lease.held")
    time.sleep(1.2)
    taken = ws.spawn("claim", "profile-erp", "--holder", "bob", "--ttl", "600").result
    assert taken["taken_over_from"] == "alice"
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    refused = ws.spawn("submit", "profile-erp", "--token", old)
    assert refused.reasons == ["tasks.lease.token"]
    assert ws.spawn("submit", "profile-erp", "--token", taken["token"]).code == 0


def test_an_expired_lease_must_be_renewed_before_submitting(ws, clock):
    ws.open("3")
    token = ws.claim("profile-erp", holder="alice", ttl=60)
    ws.write_result("profile-erp", ws.profile_patch("erp"))
    clock(61)
    assert ws.submit("profile-erp", token).reasons == ["tasks.lease.expired"]
    assert ws.task("profile-erp")["status"] == "claimed"

    # Nobody took it over, so there is still only one holder and the lease can be extended.
    renewed = ws.run(
        "claim", "profile-erp", "--holder", "alice", "--renew", "--token", token, "--ttl", "120"
    ).result
    assert renewed["renewals"] == 1 and renewed["generation"] == 1
    assert ws.submit("profile-erp", token).code == 0


def test_renewing_keeps_others_out(ws, clock):
    ws.open("3")
    token = ws.claim("profile-erp", holder="alice", ttl=60)
    clock(50)
    ws.run("claim", "profile-erp", "--holder", "alice", "--renew", "--token", token, "--ttl", "60")
    clock(50)  # the first lease would have run out by now
    ws.run("claim", "profile-erp", "--holder", "bob").refused("tasks.lease.held")
    clock(11)
    assert ws.run("claim", "profile-erp", "--holder", "bob").result["taken_over_from"] == "alice"


def test_a_takeover_is_refused_when_the_inputs_were_tampered_with(ws, clock):
    ws.open("3")
    (ws.tasks / "profile-erp" / "input" / "sample.csv").write_text("id\n1\n")
    ws.claim("profile-erp", holder="alice", ttl=60)
    (ws.tasks / "profile-erp" / "input" / "sample.csv").write_text("id\n2\n")
    clock(61)
    detail = ws.run("claim", "profile-erp", "--holder", "bob").refused("tasks.input.changed")
    assert detail["problems"][0]["path"] == "tasks/profile-erp/input"
    assert ws.task("profile-erp")["lease"]["holder"] == "alice"


# -- what a claim hands out and what it refuses --------------------------------


def test_claim_writes_the_task_description_and_pins_the_inputs(ws):
    ws.open("3", "--store-version", "commit-7")
    (ws.tasks / "profile-erp" / "input" / "sample.csv").write_text("id\n1\n")
    claimed = ws.run("claim", "profile-erp", "--holder", "alice").result
    assert claimed["paths"] == {
        "spec": "tasks/profile-erp/task.json",
        "input": "tasks/profile-erp/input",
        "output": "tasks/profile-erp/output",
        "result": "tasks/profile-erp/output/result.json",
    }
    import json

    spec = json.loads((ws.tasks / "profile-erp" / "task.json").read_text())
    assert spec["id"] == "profile-erp" and spec["kind"] == "profile" and spec["stage"] == "3"
    assert spec["reads"]["manifest"] == ["sources.erp"]
    assert spec["writes"]["zone"] == "tasks/profile-erp/output"
    assert spec["writes"]["owned_keys"] == [
        "sources.erp.profile",
        "sources.erp.refresh_period",
        "sources.erp.rate_limit_period",
    ]
    record = ws.task("profile-erp")
    assert spec["pins"] == record["pins"]
    assert spec["pins"]["store_version"] == "commit-7"
    # Nothing in the file depends on who claimed or when.
    assert "alice" not in json.dumps(spec)
    assert (
        record["spec_sha256"]
        == hashlib.sha256((ws.tasks / "profile-erp" / "task.json").read_bytes()).hexdigest()
    )
    assert len(record["input_sha256"]) == 64


def test_claim_is_refused_outside_the_open_batch(ws):
    ws.run("claim", "profile-erp", "--holder", "alice").could_not_run("tasks.task.unknown")
    ws.open("3")
    ws.run("claim", "mapping-erp", "--holder", "alice").could_not_run("tasks.task.unknown")
    ws.finish("profile-erp", ws.profile_patch("erp"))
    ws.run("claim", "profile-erp", "--holder", "bob").refused("tasks.claim.closed")
    ws.merge("profile-erp")
    ws.run("claim", "profile-erp", "--holder", "bob").refused("tasks.claim.closed")
    ws.abort()
    ws.run("claim", "profile-erp", "--holder", "bob").refused("tasks.batch.none")
    ws.open("4")
    ws.run("claim", "profile-erp", "--holder", "bob").refused("tasks.task.not_in_batch")
    ws.run("claim", "profile-sheets", "--holder", "bob").refused("tasks.task.not_in_batch")


@pytest.mark.parametrize(
    "holder", ["", " ", "a b", "x" * 65, "-lead", "new\nline", "semi;colon", "sl/ash"]
)
def test_holder_names_are_checked(ws, holder):
    ws.open("3")
    ws.run("claim", "profile-erp", "--holder", holder).could_not_run("tasks.holder.invalid")
    assert ws.task("profile-erp")["status"] == "open"


def test_lease_length_and_renew_options_are_checked(ws):
    ws.open("3")
    for ttl in ("0", "-5", "86401"):
        ws.run("claim", "profile-erp", "--holder", "a", "--ttl", ttl).could_not_run(
            "tasks.ttl.invalid"
        )
    ws.run("claim", "profile-erp", "--holder", "a", "--renew").could_not_run("tasks.usage")
    ws.run("claim", "profile-erp", "--holder", "a", "--token", "t").could_not_run("tasks.usage")
    ws.run("claim", "profile-erp", "--holder", "a", "--renew", "--token", "t").refused(
        "tasks.lease.none"
    )
    assert ws.task("profile-erp")["status"] == "open"


def test_a_claim_is_refused_when_the_task_could_no_longer_be_submitted(ws):
    ws.open("3")

    def move(manifest):
        manifest.sources["erp"].location = "postgresql://${ERP_HOST}/erp2"

    ws.edit_manifest(move)
    detail = ws.run("claim", "profile-erp", "--holder", "alice").refused("tasks.pin.manifest")
    assert detail["reasons"][0]["detail"]["reads"] == ["sources.erp"]
    assert ws.task("profile-erp")["status"] == "open"
    # A task that does not read the changed path is unaffected.
    assert ws.run("claim", "profile-sheets", "--holder", "alice").code == 0


def test_inputs_must_be_plain_files(ws, tmp_path):
    ws.open("3")
    outside = tmp_path / "outside.csv"
    outside.write_text("id\n1\n")
    (ws.tasks / "profile-erp" / "input" / "link.csv").symlink_to(outside)
    detail = ws.run("claim", "profile-erp", "--holder", "alice").refused("tasks.input.not_plain")
    assert detail["paths"] == ["link.csv"]
    (ws.tasks / "profile-erp" / "input" / "link.csv").unlink()
    assert ws.run("claim", "profile-erp", "--holder", "alice").code == 0
