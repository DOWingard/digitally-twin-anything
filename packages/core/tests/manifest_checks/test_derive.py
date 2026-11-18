"""The interval rule: what the sources and the contract of a live twin call for."""

from datetime import timedelta
from typing import Any

from dta_core.manifest import parse_manifest
from dta_core.manifest.checks import run_checks
from dta_core.manifest.derive import RULE, derive_interval

MIN = timedelta(minutes=1)


def _manifest(
    staleness: str | None = "PT1H",
    change_time: str | None = None,
    interval: str | None = None,
    **sources: dict[str, Any],
) -> Any:
    """A live twin with one question about `Tank.level`, read from source `scada`."""
    column: dict[str, Any] = {"dtype": "double", "null_fraction": 0.0, "distinct_count": 10}
    if change_time is not None:
        column["change_time"] = change_time
    declared: dict[str, Any] = {}
    for name, fields in sources.items():
        declared[name] = {"connector": "file", "location": f"file:///data/{name}", **fields}
    declared["scada"] = {
        **declared.get("scada", {"connector": "file", "location": "file:///data/scada"}),
        "profile": {
            "profiled_at": "2026-01-05T00:00:00Z",
            "datasets": {"levels": {"rows": 10, "columns": {"level": column}}},
        },
        "mappings": {
            "levels": {
                "dataset": "levels",
                "object_type": "Tank",
                "identity": ["tank"],
                "valid_time": "ts",
                "properties": {"level": {"column": "level"}},
            }
        },
    }
    question: dict[str, Any] = {"text": "How full is each tank?", "targets": ["Tank.level"]}
    if staleness is not None:
        question["staleness"] = staleness
    document: dict[str, Any] = {
        "twin": {"id": "plant", "name": "Plant"},
        "contract": {"tier": "live", "questions": {"q_level": question}},
        "sources": declared,
    }
    if interval is not None:
        document["sync"] = {"interval": interval}
    return parse_manifest(document)


def test_the_staleness_less_the_source_period_bounds_the_interval():
    """Just before a run the newest reading is one interval and one source period old."""
    found = derive_interval(_manifest("PT1H", scada={"refresh_period": "PT15M"}))
    # 60 - 15 = 45 min allowed; shortened to whole source periods: 45 min.
    assert (found.interval, found.binding, found.feasible) == (45 * MIN, "freshness_bound", True)
    assert found.inputs.freshness_bound == 45 * MIN
    assert found.inputs.min_staleness == 60 * MIN
    assert found.inputs.rule == RULE


def test_a_longer_interval_is_cut_to_whole_source_periods():
    found = derive_interval(_manifest("PT1H", scada={"refresh_period": "PT20M"}))
    # 60 - 20 = 40 min allowed; two source periods fit.
    assert (found.interval, found.feasible) == (40 * MIN, True)
    found = derive_interval(_manifest("PT50M", scada={"refresh_period": "PT20M"}))
    # 30 min allowed; one source period fits.
    assert (found.interval, found.binding) == (20 * MIN, "source_period")


def test_a_property_that_changes_fast_shortens_the_interval_to_half_its_change_time():
    found = derive_interval(
        _manifest("PT2H", change_time="PT1H", scada={"refresh_period": "PT10M"})
    )
    assert (found.interval, found.binding, found.feasible) == (30 * MIN, "change_time", True)


def test_the_interval_is_never_shorter_than_the_fastest_source_or_a_rate_limit():
    found = derive_interval(
        _manifest("PT2H", change_time="PT10M", scada={"refresh_period": "PT15M"})
    )
    assert (found.interval, found.binding, found.feasible) == (15 * MIN, "source_period", True)
    found = derive_interval(
        _manifest(
            "PT2H",
            change_time="PT10M",
            scada={"refresh_period": "PT15M"},
            feed={"refresh_period": "PT1H", "rate_limit_period": "PT30M"},
        )
    )
    assert (found.interval, found.binding, found.feasible) == (30 * MIN, "rate_period", True)


def test_a_staleness_the_sources_cannot_meet_is_said_with_the_question_and_the_source():
    found = derive_interval(_manifest("PT20M", scada={"refresh_period": "PT15M"}))
    # 20 - 15 = 5 min allowed, but the source changes every 15 min.
    assert (found.interval, found.binding, found.feasible) == (15 * MIN, "source_period", False)
    (problem,) = found.problems
    assert "question 'q_level'" in problem and "source 'scada'" in problem
    assert "at most 5 min" in problem and "15 min (source_period)" in problem


def test_without_a_staleness_the_sources_decide_and_without_anything_nothing_is_derived():
    found = derive_interval(_manifest(None, scada={"refresh_period": "PT15M"}))
    assert (found.interval, found.binding, found.feasible) == (15 * MIN, "source_period", True)
    found = derive_interval(_manifest(None))
    assert (found.interval, found.binding, found.feasible) == (None, None, False)


def _interval_findings(manifest: Any) -> list[str]:
    return [f.message for f in run_checks(manifest) if f.code == "feasible.interval"]


def test_the_check_refuses_an_interval_that_lets_data_grow_older_than_a_staleness():
    fits = _manifest("PT1H", interval="PT45M", scada={"refresh_period": "PT15M"})
    assert _interval_findings(fits) == []
    too_long = _manifest("PT1H", interval="PT1H", scada={"refresh_period": "PT15M"})
    (message,) = _interval_findings(too_long)
    assert "question 'q_level'" in message and "source 'scada'" in message
    assert "1 h 15 min" in message


def test_the_derived_interval_passes_the_check():
    manifest = _manifest(
        "PT1H", change_time="PT1H", interval="PT1M", scada={"refresh_period": "PT10M"}
    )
    found = derive_interval(manifest)
    assert found.feasible
    manifest.sync.interval = found.interval
    assert _interval_findings(manifest) == []


def _forecasting(
    horizons: dict[str, str],
    change_time: str | None,
    refresh: str | None,
    tier: str = "live",
    staleness: str | None = None,
) -> Any:
    """`_manifest` with forecast questions about the same tank level, one per horizon."""
    sources = {"scada": {"refresh_period": refresh}} if refresh is not None else {}
    manifest = _manifest(staleness, change_time=change_time, **sources)
    document = manifest.model_dump(mode="json", exclude_none=True)
    document["contract"]["tier"] = tier
    for qid, horizon in horizons.items():
        document["contract"]["questions"][qid] = {
            "kind": "predictive",
            "targets": ["Tank.level"],
            "horizon": horizon,
        }
    return parse_manifest(document)


def test_the_bike_export_derives_the_hour_its_forecast_looks_ahead():
    """Rule 3 h 7 min 30 s from a change time of 6 h 15 min; the horizon of 1 h is the grid."""
    found = derive_interval(
        _forecasting({"q_ahead": "PT1H"}, change_time="PT6H15M", refresh="PT18S", tier="static")
    )
    assert found.unfitted == timedelta(hours=3, minutes=7, seconds=30)
    assert (found.interval, found.binding, found.feasible) == (60 * MIN, "horizon", True)
    assert found.horizon == found.term("horizon") == 60 * MIN
    assert found.questions == ("q_ahead",)
    # Recorded under sync.derivation are the rule's own inputs only: a field the
    # manifest schema did not have would be refused by a runtime built before it.
    assert "horizon" not in found.inputs.model_dump()


def test_the_water_utility_keeps_its_quarter_hour():
    """Staleness 30 min, source every 15 min: the rule's 15 min divides the 1 h horizon."""
    found = derive_interval(
        _forecasting({"q_ahead": "PT1H"}, change_time="PT4H30M", refresh="PT15M", staleness="PT30M")
    )
    # The freshness bound and the source period are both 15 min; the source is named.
    assert (found.interval, found.binding, found.feasible) == (15 * MIN, "source_period", True)
    assert found.unfitted == found.interval


def test_an_interval_that_does_not_divide_the_horizon_is_shortened_until_it_does():
    """25 min: 60 / ceil(60 / 25) = 20 min."""
    found = derive_interval(_forecasting({"q_ahead": "PT1H"}, change_time="PT50M", refresh="PT1M"))
    assert found.unfitted == 25 * MIN
    assert (found.interval, found.binding, found.feasible) == (20 * MIN, "horizon", True)


def test_several_horizons_are_divided_by_their_greatest_common_divisor():
    found = derive_interval(
        _forecasting({"q_a": "PT1H", "q_b": "PT1H30M"}, change_time="PT2H", refresh="PT1M")
    )
    # gcd(60, 90) = 30 min; the rule's 1 h is fitted to it.
    assert (found.interval, found.binding) == (30 * MIN, "horizon")
    assert found.horizon == 30 * MIN and found.questions == ("q_a", "q_b")


def test_a_fit_that_leaves_whole_milliseconds_only_takes_the_next_divisor():
    found = derive_interval(_forecasting({"q_ahead": "PT1H"}, change_time="PT18M", refresh="PT1M"))
    # 9 min: 7 steps do not divide the hour in whole milliseconds; 8 steps do.
    assert found.unfitted == 9 * MIN
    assert found.interval == timedelta(minutes=7, seconds=30)


def test_a_horizon_shorter_than_the_source_period_cannot_be_met():
    found = derive_interval(
        _forecasting({"q_ahead": "PT30M"}, change_time="PT6H", refresh="PT1H", tier="static")
    )
    assert (found.interval, found.binding, found.feasible) == (30 * MIN, "horizon", False)
    (problem,) = found.problems
    assert "'q_ahead' (30 min)" in problem and "source_period 1 h" in problem


def test_horizons_alone_give_an_interval():
    found = derive_interval(_forecasting({"q_ahead": "PT6H"}, change_time=None, refresh=None))
    assert (found.interval, found.binding, found.feasible) == (360 * MIN, "horizon", True)
