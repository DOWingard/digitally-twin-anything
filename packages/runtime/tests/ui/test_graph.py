"""The graph is bounded and says so; a click shows values and their provenance at the version."""

from __future__ import annotations

from api_testkit import link, node
from dta_runtime.ui import ids
from dta_runtime.ui.graph import NODE_LIMIT
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from ui_page import (
    choose_dropdown,
    choose_object,
    click_graph_element,
    graph_nodes,
    graph_status,
    open_page,
    select_version,
    text_of,
    wait_for,
    wait_graph_at,
)
from ui_testkit import READINGS, READINGS_SENSOR

SUBGRAPH_PATH = "/api/v1/graph/subgraph"


def _around(browser, uid, typed=None):
    choose_object(browser, uid, typed=typed or uid.split(":", 1)[1])
    wait_for(browser, lambda: f"around {uid}" in graph_status(browser), f"the graph around {uid}")


def test_graph_stops_at_its_node_limit_and_says_so(stack, browser):
    open_page(browser, stack.ui_url)
    head = max(stack.history.versions)
    wait_graph_at(browser, head)
    mark = stack.api_requests.mark()
    # The sensor's readings alone are more than the default limit. Its readings' uids hold
    # its key too, and come first in uid order: the sensor is searched for by its uid.
    _around(browser, READINGS_SENSOR, typed=READINGS_SENSOR)
    assert READINGS > NODE_LIMIT
    nodes = wait_for(browser, lambda: len(graph_nodes(browser)) == NODE_LIMIT, "the bounded graph")
    assert nodes
    status = graph_status(browser)
    assert f"{NODE_LIMIT} nodes" in status
    assert f"Limit of {NODE_LIMIT} nodes reached" in status
    asked = [
        request
        for request in stack.api_requests.since(mark, SUBGRAPH_PATH)
        if request.json()["seeds"] == [READINGS_SENSOR]
    ]
    assert asked and asked[-1].json()["limit"] == NODE_LIMIT
    assert asked[-1].value("at") == stack.history.versions[head].id

    limit = browser.find_element("id", ids.GRAPH_LIMIT)
    limit.send_keys(Keys.CONTROL, "a")
    limit.send_keys("20", Keys.ENTER)
    wait_for(browser, lambda: len(graph_nodes(browser)) == 20, "a graph of 20 nodes")
    assert "Limit of 20 nodes reached" in graph_status(browser)

    # Below the limit nothing is said about it.
    limit.send_keys(Keys.CONTROL, "a")
    limit.send_keys(str(NODE_LIMIT), Keys.ENTER)
    _around(browser, node("J2"))
    wait_for(browser, lambda: len(graph_nodes(browser)) == 5, "the neighbourhood of J2")
    assert "Limit" not in graph_status(browser)


def test_a_click_shows_values_and_their_provenance(stack, browser):
    open_page(browser, stack.ui_url)
    head = max(stack.history.versions)
    wait_graph_at(browser, head)
    _around(browser, node("J2"))
    wait_for(browser, lambda: node("J2") in graph_nodes(browser), "J2 drawn")

    click_graph_element(browser, node("J2"))
    details = wait_for(
        browser,
        lambda: "elevation" in (text := text_of(browser, ids.GRAPH_DETAILS)) and text,
        "the values of J2",
    )
    assert f"as of #{head}" in details
    assert "45.0 m" in details  # elevation with its unit
    assert "gis" in details and "scada" in details and "observed" in details

    click_graph_element(browser, f"Pipe|{link('P1')}|{node('J1')}|{node('J2')}")
    pipe = wait_for(
        browser,
        lambda: "length" in (text := text_of(browser, ids.GRAPH_DETAILS)) and text,
        "the values of pipe P1",
    )
    assert link("P1") in pipe and "100.0 m" in pipe and "200.0 mm" in pipe

    click_graph_element(browser, f"Sensor.measures||sensor:S1|{node('J2')}")
    reference = wait_for(
        browser,
        lambda: "measures" in (text := text_of(browser, ids.GRAPH_DETAILS)) and text,
        "the reference held by S1",
    )
    assert node("J2") in reference and "sensor:S1" in reference

    # The values and their provenance follow the selected version.
    click_graph_element(browser, node("J2"))
    wait_for(browser, lambda: "elevation" in text_of(browser, ids.GRAPH_DETAILS), "J2 again")
    select_version(browser, 9)
    older = wait_for(
        browser,
        lambda: "as of #9" in (text := text_of(browser, ids.GRAPH_DETAILS)) and text,
        "the values of J2 at #9",
    )
    assert "2026-01-05 09:00:00 UTC" in older  # demand as observed at #9
    assert "2026-01-05 09:00:00 UTC" not in details


def _node_types(browser):
    return browser.execute_script(
        "return document.getElementById(arguments[0])._cyreg.cy.nodes().map(n => n.data('type'))",
        ids.GRAPH_VIEW,
    )


def test_graph_filters_by_depth_and_type(stack, browser):
    open_page(browser, stack.ui_url)
    wait_graph_at(browser, max(stack.history.versions))
    _around(browser, node("J2"))
    wait_for(browser, lambda: len(graph_nodes(browser)) == 5, "the neighbourhood of J2")

    mark = stack.api_requests.mark()
    browser.find_element(
        By.XPATH, f"//*[@id='{ids.GRAPH_DEPTH}']//label[normalize-space()='2']"
    ).click()
    wait_for(browser, lambda: "2 hops deep" in graph_status(browser), "two hops")
    two_hops = wait_for(
        browser, lambda: len(graph_nodes(browser)) > 5 and graph_nodes(browser), "more nodes"
    )
    assert {node("R1"), node("J4")} <= set(two_hops)
    assert any(
        request.json() and request.json()["depth"] == 2
        for request in stack.api_requests.since(mark, SUBGRAPH_PATH)
    )

    mark = stack.api_requests.mark()
    choose_dropdown(browser, ids.GRAPH_NODE_TYPES, "Junction", typed="Junc")
    browser.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    wait_for(browser, lambda: set(_node_types(browser)) == {"Junction"}, "only junctions")
    assert any(
        request.json() and request.json().get("node_types") == ["Junction"]
        for request in stack.api_requests.since(mark, SUBGRAPH_PATH)
    )
