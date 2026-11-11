"""The options of an HTTP source: defaults, and malformed entries named by their path."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest
from dta_core.connect import open_connector
from dta_core.connect.http import parse_options
from dta_core.manifest.models import Source
from dta_core.sources import SourceConfigError


def test_defaults() -> None:
    settings = parse_options({"datasets": {"r": {"path": "/r", "since": {"param": "start"}}}})
    assert (settings.timeout_s, settings.max_attempts, settings.max_wait_s) == (30.0, 5, 60.0)
    assert settings.headers == {} and settings.clock is None
    spec = settings.datasets["r"]
    assert spec.paging.type == "none" and spec.records is None and spec.columns is None
    assert spec.since is not None and spec.since.format == "iso"


def test_paging_defaults() -> None:
    datasets = {
        "c": {"path": "/c", "paging": {"type": "cursor", "cursor_path": "next"}},
        "p": {"path": "/p", "paging": {"type": "page"}},
        "o": {"path": "/o", "paging": {"type": "offset", "limit": 50}},
        "l": {"path": "/l", "paging": {"type": "link"}},
    }
    paging = {
        name: spec.paging for name, spec in parse_options({"datasets": datasets}).datasets.items()
    }
    assert paging["c"].cursor_param == "cursor"
    assert (paging["p"].page_param, paging["p"].first_page, paging["p"].total_path) == (
        "page",
        1,
        None,
    )
    assert (paging["o"].offset_param, paging["o"].limit_param, paging["o"].limit) == (
        "offset",
        "limit",
        50,
    )
    assert paging["l"].next_url_path is None


def test_column_types_are_the_file_connector_names() -> None:
    columns = {"a": "int32", "b": "timestamp[ms, tz=Europe/Berlin]", "c": "large_string"}
    spec = parse_options({"datasets": {"r": {"path": "/r", "columns": columns}}}).datasets["r"]
    assert spec.columns == {
        "a": pa.int32(),
        "b": pa.timestamp("ms", tz="Europe/Berlin"),
        "c": pa.large_string(),
    }


def _dataset(**entry: Any) -> dict[str, Any]:
    return {"datasets": {"a": {"path": "/a", **entry}}}


@pytest.mark.parametrize(
    ("options", "option"),
    [
        ({"bogus": 1, "datasets": {"a": {"path": "/a"}}}, "bogus"),
        ({}, "datasets"),
        ({"datasets": {}}, "datasets"),
        ({"datasets": []}, "datasets"),
        ({"datasets": {"a": {}}}, "datasets.a.path"),
        ({"datasets": {"a": {"path": "http://elsewhere/a"}}}, "datasets.a.path"),
        ({"datasets": {"a": {"path": "//elsewhere/a"}}}, "datasets.a.path"),
        ({"datasets": {"a": {"path": "/a?b=1"}}}, "datasets.a.path"),
        ({"datasets": {"a": {"path": "/a/../b"}}}, "datasets.a.path"),
        ({"datasets": {"a": {"path": "/a", "method": "POST"}}}, "datasets.a.method"),
        (_dataset(records="bad["), "datasets.a.records"),
        (_dataset(params={"x": {"y": 1}}), "datasets.a.params.x"),
        (_dataset(params={"x": []}), "datasets.a.params.x"),
        (_dataset(paging={"type": "scroll"}), "datasets.a.paging.type"),
        (_dataset(paging={}), "datasets.a.paging.type"),
        (_dataset(paging={"type": "cursor"}), "datasets.a.paging.cursor_path"),
        (_dataset(paging={"type": "page", "cursor_path": "x"}), "datasets.a.paging.cursor_path"),
        (_dataset(paging={"type": "page", "first_page": -1}), "datasets.a.paging.first_page"),
        (_dataset(paging={"type": "offset"}), "datasets.a.paging.limit"),
        (_dataset(paging={"type": "offset", "limit": 0}), "datasets.a.paging.limit"),
        (
            _dataset(
                paging={"type": "offset", "limit": 5, "offset_param": "n", "limit_param": "n"}
            ),
            "datasets.a.paging.limit_param",
        ),
        (
            _dataset(paging={"type": "cursor", "cursor_path": "c"}, params={"cursor": "x"}),
            "datasets.a.params.cursor",
        ),
        (
            _dataset(paging={"type": "page"}, since={"param": "page"}),
            "datasets.a.since.param",
        ),
        (_dataset(since={"param": "start"}, params={"start": "x"}), "datasets.a.params.start"),
        (_dataset(since={}), "datasets.a.since.param"),
        (_dataset(since={"param": "start", "format": "rfc2822"}), "datasets.a.since.format"),
        (_dataset(columns={}), "datasets.a.columns"),
        (_dataset(columns={"t": "moment"}), "datasets.a.columns.t"),
        (_dataset(columns={"t": "json"}), "datasets.a.columns.t"),
        ({**_dataset(), "headers": {"Bad Name": "x"}}, "headers.Bad Name"),
        ({**_dataset(), "headers": {"X-Key": {"v": 1}}}, "headers.X-Key"),
        ({**_dataset(), "headers": {"X-Key": ""}}, "headers.X-Key"),
        ({**_dataset(), "clock": {"path": "/t"}}, "clock.field"),
        ({**_dataset(), "clock": {"field": "time"}}, "clock.path"),
        ({**_dataset(), "clock": {"path": "/t", "field": "time", "tz": "UTC"}}, "clock.tz"),
        ({**_dataset(), "timeout_s": 0}, "timeout_s"),
        ({**_dataset(), "max_attempts": 0}, "max_attempts"),
        ({**_dataset(), "max_attempts": 2.5}, "max_attempts"),
        ({**_dataset(), "max_attempts": True}, "max_attempts"),
        ({**_dataset(), "max_wait_s": -1}, "max_wait_s"),
    ],
)
def test_malformed_options_name_the_option(options: dict[str, Any], option: str) -> None:
    with pytest.raises(SourceConfigError) as caught:
        parse_options(options)
    assert caught.value.detail["option"] == option
    assert option in caught.value.message


def test_the_factory_reports_the_option_and_the_source() -> None:
    entry = Source(
        connector="http",
        location="http://plant:8080",
        options=_dataset(paging={"type": "scroll"}),
    )
    with pytest.raises(SourceConfigError) as caught:
        open_connector("plant", entry, {})
    assert caught.value.detail["option"] == "datasets.a.paging.type"
    assert caught.value.detail["source"] == "plant"
    assert caught.value.detail["kind"] == "http"
