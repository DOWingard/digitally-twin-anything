"""The interface starts and answers its health check whether or not the API is up; the
page then says the API cannot be reached instead of failing."""

from __future__ import annotations

import secrets

import httpx
import pytest
from dta_runtime.settings import UiSettings
from dta_runtime.ui import create_app, ids
from ui_page import text_of, wait_for
from ui_testkit import UiServer, free_port, release_port


@pytest.fixture
def without_api(tmp_path):
    """The interface configured for an API that is not there: a port nothing listens on."""
    unused = free_port()
    settings = UiSettings(
        api_url=f"http://127.0.0.1:{unused}",
        api_token=secrets.token_urlsafe(32),
        logs_dir=tmp_path,
    )
    try:
        yield create_app(settings), settings
    finally:
        release_port(unused)


def test_healthz_answers_without_the_api(without_api):
    app, _ = without_api
    answer = app.server.test_client().get("/healthz")
    assert answer.status_code == 200
    assert answer.get_json() == {"status": "ok"}

    with UiServer(app.server, free_port()) as ui:
        served = httpx.get(f"http://127.0.0.1:{ui.port}/healthz", timeout=5.0)
    assert served.status_code == 200
    assert served.json() == {"status": "ok"}


def test_page_says_the_api_cannot_be_reached(without_api, browser):
    app, settings = without_api
    with UiServer(app.server, free_port()) as ui:
        browser.get(f"http://127.0.0.1:{ui.port}/")
        problem = wait_for(
            browser, lambda: text_of(browser, ids.HEALTH_FEEDBACK), "the problem to be shown"
        )
        assert "The API cannot be reached" in problem
        assert "ui.api_unreachable" in problem
        assert "trace id" in problem
        assert "Traceback" not in browser.page_source
        assert settings.api_token not in browser.page_source
