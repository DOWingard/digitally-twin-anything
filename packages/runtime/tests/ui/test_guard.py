"""The interface answers only requests addressed to it, and takes a change only from its
own pages: a page of another site cannot act through the token the interface holds."""

from __future__ import annotations

import json
import secrets

import httpx
import pytest
from dta_runtime.settings import UiSettings
from dta_runtime.ui import create_app, ids
from dta_runtime.ui.guard import HOST_REFUSED, ORIGIN_REFUSED, same_origin
from ui_page import text_of, wait_for
from ui_testkit import UiServer, free_port, release_port

CALLBACK = "/_dash-update-component"


@pytest.fixture
def app(tmp_path):
    """The interface for an API that is not there: no test here needs the twin."""
    unused = free_port()
    settings = UiSettings(
        api_url=f"http://127.0.0.1:{unused}",
        api_token=secrets.token_urlsafe(32),
        logs_dir=tmp_path,
    )
    try:
        yield create_app(settings)
    finally:
        release_port(unused)


def refusal(answer) -> dict:
    assert answer.status_code == 403
    assert answer.headers["Content-Type"].startswith("application/problem+json")
    document = json.loads(answer.get_data())
    assert document["status"] == 403
    assert document["type"] == f"urn:dta:error:{document['code']}"
    return document


@pytest.mark.parametrize("host", ["127.0.0.1:18761", "localhost:8050", "localhost", "127.0.0.1"])
def test_a_loopback_name_is_served(app, host):
    answer = app.server.test_client().get("/healthz", headers={"Host": host})
    assert answer.status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "twin.example:18761",
        "127.0.0.1.twin.example",
        "localhost.twin.example:8050",
        "twin.example",
        "ui:8050",
        "",
    ],
)
@pytest.mark.parametrize("path", ["/", "/healthz", "/_dash-layout", "/no-such-page"])
def test_another_host_is_refused_whatever_it_asks_for(app, host, path):
    answer = app.server.test_client().get(path, headers={"Host": host})
    assert refusal(answer)["code"] == HOST_REFUSED


def test_a_callback_for_another_host_is_refused_before_it_runs(app):
    answer = app.server.test_client().post(
        CALLBACK,
        json={"output": "x.children", "outputs": {}, "inputs": [], "changedPropIds": []},
        headers={"Host": "twin.example:18761", "Origin": "http://twin.example:18761"},
    )
    assert refusal(answer)["code"] == HOST_REFUSED


@pytest.mark.parametrize(
    "origin",
    [
        "http://twin.example",
        "http://twin.example:18761",
        # Another program's page on this machine.
        "http://127.0.0.1:3000",
        "http://localhost:18761",
        "https://127.0.0.1:18761.twin.example",
        "null",
        "",
    ],
)
def test_a_change_asked_by_another_origin_is_refused(app, origin):
    answer = app.server.test_client().post(
        CALLBACK,
        json={},
        headers={"Host": "127.0.0.1:18761", "Origin": origin},
    )
    assert refusal(answer)["code"] == ORIGIN_REFUSED


def test_a_change_from_the_interface_s_own_page_reaches_the_callback(app):
    client = app.server.test_client()
    for headers in (
        {"Host": "127.0.0.1:18761", "Origin": "http://127.0.0.1:18761"},
        {"Host": "localhost:18761", "Origin": "http://LOCALHOST:18761"},
        # No origin stated: not a browser's cross-site request.
        {"Host": "127.0.0.1:18761"},
    ):
        answer = client.post(CALLBACK, json={}, headers=headers)
        # Dash itself answers: the body is not a callback it knows.
        assert answer.status_code != 403, headers


def test_a_read_states_no_origin_and_is_served_from_anywhere_it_is_linked(app):
    answer = app.server.test_client().get(
        "/healthz", headers={"Host": "127.0.0.1:18761", "Origin": "http://twin.example"}
    )
    assert answer.status_code == 200


@pytest.mark.parametrize(
    ("origin", "host", "same"),
    [
        ("http://127.0.0.1:18761", "127.0.0.1:18761", True),
        ("http://localhost:18761", "LOCALHOST:18761", True),
        ("http://127.0.0.1:18761", "127.0.0.1:18762", False),
        ("http://127.0.0.1", "127.0.0.1:18761", False),
        ("ftp://127.0.0.1:18761", "127.0.0.1:18761", False),
        ("127.0.0.1:18761", "127.0.0.1:18761", False),
        ("http://[", "127.0.0.1:18761", False),
        ("null", "127.0.0.1:18761", False),
    ],
)
def test_same_origin(origin, host, same):
    assert same_origin(origin, host) is same


def test_the_served_page_works_under_both_names_and_not_under_a_third(app, browser):
    with UiServer(app.server, free_port()) as ui:
        for name in ("127.0.0.1", "localhost"):
            browser.get(f"http://{name}:{ui.port}/")
            # The page's callbacks ran: it reports that the API is not there.
            problem = wait_for(
                browser, lambda: text_of(browser, ids.HEALTH_FEEDBACK), "the page's callbacks"
            )
            assert "ui.api_unreachable" in problem, name
        refused = httpx.get(
            f"http://127.0.0.1:{ui.port}/", headers={"Host": f"twin.example:{ui.port}"}, timeout=5
        )
    assert refused.status_code == 403
    assert refused.json()["code"] == HOST_REFUSED
