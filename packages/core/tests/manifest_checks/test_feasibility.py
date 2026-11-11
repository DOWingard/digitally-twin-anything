"""Feasibility of the sync interval against sources that change at different rates."""

from dta_core.manifest import parse_manifest
from dta_core.manifest.checks import run_checks


def _manifest(interval: str, **periods: dict[str, str]) -> dict:
    sources = {
        name: {"connector": "file", "location": f"file:///data/{name}", **fields}
        for name, fields in periods.items()
    }
    return {
        "twin": {"id": "plant", "name": "Plant"},
        "contract": {"tier": "live"},
        "sources": sources,
        "sync": {"interval": interval},
    }


def _interval_findings(document: dict) -> list[str]:
    findings = run_checks(parse_manifest(document))
    return [finding.message for finding in findings if finding.code == "feasible.interval"]


def test_the_interval_follows_the_fastest_source():
    """A slower source is only read more often than it changes; that is not a finding."""
    document = _manifest("PT15M", fast={"refresh_period": "PT15M"}, slow={"refresh_period": "PT1H"})
    assert _interval_findings(document) == []


def test_an_interval_shorter_than_every_source_changes_is_one_finding():
    document = _manifest("PT5M", fast={"refresh_period": "PT15M"}, slow={"refresh_period": "PT1H"})
    (message,) = _interval_findings(document)
    assert "refresh period 15 min of source 'fast'" in message


def test_a_rate_limit_binds_whichever_source_has_it():
    document = _manifest(
        "PT15M",
        fast={"refresh_period": "PT15M"},
        slow={"refresh_period": "PT1H", "rate_limit_period": "PT30M"},
    )
    (message,) = _interval_findings(document)
    assert "rate limit period 30 min of source 'slow'" in message
