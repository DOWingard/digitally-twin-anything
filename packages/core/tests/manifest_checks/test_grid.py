"""The time grid: every twin with a forecast needs sync.interval, whatever its tier, and the
horizons and backtest steps of its forecasts are whole multiples of it."""

from __future__ import annotations

from typing import Any

from dta_core.manifest import parse_manifest
from dta_core.manifest.checks import run_checks

GRID = ("grid.no_interval", "grid.horizon", "grid.backtest_step")


def _bike_export(**sync: Any) -> dict[str, Any]:
    """A static twin over a one-off export with an hourly forecast, reduced from the bike
    twin whose interval was written by hand before the tool could check it."""
    document: dict[str, Any] = {
        "twin": {"id": "cykelbo", "name": "Bike stations"},
        "contract": {
            "tier": "static",
            "questions": {
                "q_bikes_ahead": {
                    "kind": "predictive",
                    "targets": ["Station.bikes"],
                    "horizon": "PT1H",
                    "model": "m_bikes",
                }
            },
        },
        "models": {
            "m_bikes": {
                "question": "q_bikes_ahead",
                "target": "Station.bikes",
                "backtest": {"initial_window": "P14D", "step": "PT6H", "min_folds": 3},
            }
        },
    }
    if sync:
        document["sync"] = sync
    return document


def _grid(document: dict[str, Any]) -> list[tuple[str, str, str]]:
    findings = run_checks(parse_manifest(document))
    return [(f.code, f.path, f.message) for f in findings if f.code in GRID]


def test_a_static_forecast_without_an_interval_is_an_error():
    ((code, path, message),) = _grid(_bike_export(backfill_interval="PT1H"))
    assert (code, path) == ("grid.no_interval", "sync.interval")
    assert "q_bikes_ahead" in message and "dta sync derive --write" in message


def test_a_model_alone_needs_the_interval_too():
    document = _bike_export()
    document["contract"]["questions"] = {}
    document["models"]["m_bikes"]["question"] = None
    ((code, _path, message),) = _grid(document)
    assert code == "grid.no_interval" and "models.m_bikes" in message


def test_a_twin_without_forecasts_needs_no_interval():
    document = _bike_export()
    document["contract"]["questions"]["q_bikes_ahead"]["kind"] = "descriptive"
    del document["models"]
    assert _grid(document) == []


def test_a_horizon_off_the_grid_is_an_error_naming_the_fix():
    ((code, path, message),) = _grid(_bike_export(interval="PT40M"))
    assert (code, path) == ("grid.horizon", "contract.questions.q_bikes_ahead.horizon")
    assert "1 h" in message and "40 min" in message and "dta sync derive --write" in message


def test_a_backtest_step_off_the_grid_names_the_next_multiple():
    document = _bike_export(interval="PT20M")
    document["models"]["m_bikes"]["backtest"]["step"] = "PT50M"
    ((code, path, message),) = _grid(document)
    assert (code, path) == ("grid.backtest_step", "models.m_bikes.backtest.step")
    assert "such as 1 h" in message


def test_the_hand_written_interval_of_the_bike_twin_is_on_its_grid():
    """PT15M divides the hour and six hours: nothing to report, though derive gives PT1H."""
    assert _grid(_bike_export(interval="PT15M")) == []


def test_a_static_twin_keeps_its_interval_without_a_warning():
    findings = run_checks(parse_manifest(_bike_export(interval="PT1H")))
    assert [f for f in findings if f.code == "tier.static_extras"] == []


def test_interval_bounds_of_a_static_twin_without_forecasts_are_still_warned():
    document = _bike_export(interval="PT1H", minimum_interval="PT1H")
    document["contract"]["questions"]["q_bikes_ahead"]["kind"] = "descriptive"
    del document["models"]
    found = [
        (f.code, f.path) for f in run_checks(parse_manifest(document))
        if f.code == "tier.static_extras"
    ]  # fmt: skip
    assert found == [("tier.static_extras", "sync")]
