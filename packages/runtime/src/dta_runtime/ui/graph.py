"""The graph tab: a bounded part of the twin's graph at the selected version.

The part shown is the API's subgraph around one searched object, or around
the objects of one type, a number of hops deep, cut at a node limit the page
states when it is reached. Clicking a node or an edge reads that object (for
an edge, the link object, or for a reference the object holding it) with the
provenance of each of its values.
"""

from __future__ import annotations

from typing import Any

import dash_cytoscape as cyto
from dash import Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from . import ids
from .backend import Backend
from .feedback import guarded, note
from .schema import Schema
from .text import plural, time_text, value_text, with_unit

NODE_LIMIT = 300
NODE_LIMIT_MAX = 5_000
# The API takes at most this many seeds in one subgraph request.
SEEDS_MAX = 1_000
DEPTHS = (1, 2, 3, 4)
SEARCH_PER_TYPE = 10
SEARCH_TYPES_MAX = 50


def layout() -> html.Div:
    return html.Div(
        [
            html.Div(
                [
                    html.Label("Around one object", htmlFor=ids.GRAPH_OBJECT),
                    dcc.Dropdown(
                        id=ids.GRAPH_OBJECT,
                        options=[],
                        placeholder="search by uid…",
                        searchable=True,
                        clearable=True,
                        debounce=True,
                    ),
                    html.Div(id=ids.GRAPH_SEARCH_FEEDBACK),
                    html.Label("or around all objects of a type", htmlFor=ids.GRAPH_TYPE),
                    dcc.Dropdown(id=ids.GRAPH_TYPE, options=[], clearable=False),
                    html.Label("Show only these types", htmlFor=ids.GRAPH_NODE_TYPES),
                    dcc.Dropdown(
                        id=ids.GRAPH_NODE_TYPES, options=[], multi=True, placeholder="all types"
                    ),
                    html.Label("Depth (hops)"),
                    dcc.RadioItems(
                        id=ids.GRAPH_DEPTH,
                        options=[{"label": f" {depth}", "value": depth} for depth in DEPTHS],
                        value=1,
                        inline=True,
                        className="radio",
                    ),
                    html.Label("Most nodes shown", htmlFor=ids.GRAPH_LIMIT),
                    dcc.Input(
                        id=ids.GRAPH_LIMIT,
                        type="number",
                        min=1,
                        max=NODE_LIMIT_MAX,
                        step=1,
                        value=NODE_LIMIT,
                        debounce=True,
                    ),
                    html.Div(id=ids.GRAPH_STATUS, className="status"),
                    html.Div(id=ids.GRAPH_LEGEND, className="legend"),
                ],
                className="controls",
            ),
            html.Div(
                [
                    html.Div(id=ids.GRAPH_FEEDBACK),
                    cyto.Cytoscape(
                        id=ids.GRAPH_VIEW,
                        elements=[],
                        layout={"name": "cose", "animate": False},
                        stylesheet=[],
                        className="graph-view",
                        minZoom=0.1,
                        maxZoom=4,
                    ),
                ],
                className="graph-main",
            ),
            html.Div(
                [html.Div(id=ids.GRAPH_DETAILS_FEEDBACK), html.Div(id=ids.GRAPH_DETAILS)],
                className="details",
            ),
        ],
        className="graph-tab",
    )


def stylesheet(schema: Schema) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = [
        {
            "selector": "node",
            "style": {
                "label": "data(label)",
                "font-size": 7,
                "color": "#222",
                "text-valign": "bottom",
                "text-margin-y": 2,
                "width": 14,
                "height": 14,
                "background-color": "#888",
                "border-width": 1,
                "border-color": "#fff",
            },
        },
        {
            "selector": "edge",
            "style": {
                "width": 1.5,
                "line-color": "#9aa",
                "target-arrow-color": "#9aa",
                "target-arrow-shape": "triangle",
                "arrow-scale": 0.6,
                "curve-style": "bezier",
            },
        },
        {"selector": 'edge[kind = "reference"]', "style": {"line-style": "dashed"}},
    ]
    for name in schema.node_types():
        rules.append(
            {
                "selector": f"node[type = {_quoted(name)}]",
                "style": {"background-color": schema.colour(name)},
            }
        )
    rules += [
        {"selector": "node.seed", "style": {"border-width": 3, "border-color": "#111"}},
        {
            "selector": ":selected",
            "style": {"border-width": 3, "border-color": "#d00", "line-color": "#d00"},
        },
    ]
    return rules


def _quoted(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def elements(document: dict[str, Any], seeds: set[str]) -> tuple[list[dict[str, Any]], int]:
    """Cytoscape elements of a subgraph answer, and how many edges there are among the nodes.

    Edges whose ends are not both among the nodes (a node type filter or the
    limit left one out) are dropped: the graph view cannot draw them.
    """
    nodes = document.get("nodes", [])
    present = {node["uid"] for node in nodes}
    found: list[dict[str, Any]] = [
        {
            "data": {
                "id": node["uid"],
                "label": node["uid"],
                "type": node.get("type"),
                "depth": node.get("depth"),
            },
            "classes": "seed" if node["uid"] in seeds else "",
        }
        for node in nodes
    ]
    edges = 0
    for edge in document.get("edges", []):
        if edge["src"] not in present or edge["dst"] not in present:
            continue
        link_uid = edge.get("link_uid")
        data: dict[str, Any] = {
            "id": f"{edge['edge_type']}|{link_uid or ''}|{edge['src']}|{edge['dst']}",
            "source": edge["src"],
            "target": edge["dst"],
            "edge_type": edge["edge_type"],
            "kind": "link" if link_uid else "reference",
        }
        if link_uid:
            data["link_uid"] = link_uid
        found.append({"data": data})
        edges += 1
    return found, edges


def legend(schema: Schema, present: set[str]) -> list[Any]:
    return [
        html.Span(
            [html.Span(className="swatch", style={"background": schema.colour(name)}), name],
            className="legend-item",
        )
        for name in schema.node_types()
        if name in present
    ]


def details(
    schema: Schema,
    found: dict[str, Any],
    version: dict[str, Any],
    *,
    heading: list[Any],
    only: str | None = None,
) -> html.Div:
    """An object's values with their units and the provenance of each, at the version."""
    type_name = found["type"]
    values: dict[str, Any] = found.get("values", {})
    provenance: dict[str, Any] = found.get("provenance") or {}
    fallback = provenance.get("*")
    names = [only] if only else list(values)
    rows = []
    for name in names:
        source = provenance.get(name, fallback) or {}
        rows.append(
            html.Tr(
                [
                    html.Td(name),
                    html.Td(with_unit(value_text(values.get(name)), schema.unit(type_name, name))),
                    html.Td(source.get("source", "")),
                    html.Td(source.get("kind", "")),
                    html.Td(value_text(source.get("confidence"))),
                    html.Td(time_text(source.get("t_valid"))),
                ]
            )
        )
    return html.Div(
        [
            html.H3(heading),
            html.Div(
                [
                    html.Code(found["uid"]),
                    f" · {type_name} · as of #{version['seq']} ({time_text(version['t'])})",
                ],
                className="muted",
            ),
            html.Table(
                [
                    html.Thead(
                        html.Tr(
                            [
                                html.Th(label)
                                for label in (
                                    "property",
                                    "value",
                                    "source",
                                    "kind",
                                    "confidence",
                                    "valid from",
                                )
                            ]
                        )
                    ),
                    html.Tbody(rows),
                ],
                className="table properties",
            ),
        ]
    )


def register(app: Dash, backend: Backend) -> None:
    @app.callback(
        Output(ids.GRAPH_TYPE, "options"),
        Output(ids.GRAPH_TYPE, "value"),
        Output(ids.GRAPH_NODE_TYPES, "options"),
        Output(ids.GRAPH_NODE_TYPES, "value"),
        Output(ids.GRAPH_FEEDBACK, "children", allow_duplicate=True),
        Input(ids.VERSION, "data"),
        State(ids.GRAPH_TYPE, "value"),
        State(ids.GRAPH_NODE_TYPES, "value"),
        prevent_initial_call="initial_duplicate",
    )
    @guarded(4)
    def type_choices(
        version: dict[str, Any] | None, chosen: str | None, shown: list[str] | None
    ) -> Any:
        if not version:
            raise PreventUpdate
        schema = backend.schema(version["id"])
        options = [
            {"label": schema.type_label(name), "value": name} for name in schema.node_types()
        ]
        value = chosen if schema.has(chosen) else schema.default_node_type()
        kept = [name for name in (shown or []) if schema.has(name)]
        return (
            options,
            value if value != chosen else no_update,
            options,
            kept if kept != (shown or []) else no_update,
        )

    @app.callback(
        Output(ids.GRAPH_OBJECT, "options"),
        Output(ids.GRAPH_SEARCH_FEEDBACK, "children"),
        Input(ids.GRAPH_OBJECT, "search_value"),
        State(ids.GRAPH_OBJECT, "value"),
        State(ids.VERSION, "data"),
    )
    @guarded(1)
    def search(text: str | None, chosen: str | None, version: dict[str, Any] | None) -> Any:
        if not text or not version:
            raise PreventUpdate
        schema = backend.schema(version["id"])
        found: list[str] = []
        pattern = f"%{text.strip()}%"
        for name in schema.node_types()[:SEARCH_TYPES_MAX]:
            document = backend.client.get(
                "/objects/{type}",
                path={"type": name},
                params={
                    "at": version["id"],
                    "columns": "uid",
                    "where": [f"uid:like:{pattern}"],
                    "limit": SEARCH_PER_TYPE,
                },
            )
            found += [row["uid"] for row in document.get("data", [])]
        options = [{"label": uid, "value": uid} for uid in found]
        if chosen and chosen not in found:
            options.insert(0, {"label": chosen, "value": chosen})
        return options

    @app.callback(
        Output(ids.GRAPH_VIEW, "elements"),
        Output(ids.GRAPH_VIEW, "stylesheet"),
        Output(ids.GRAPH_STATUS, "children"),
        Output(ids.GRAPH_LEGEND, "children"),
        Output(ids.GRAPH_FEEDBACK, "children"),
        Input(ids.VERSION, "data"),
        Input(ids.GRAPH_OBJECT, "value"),
        Input(ids.GRAPH_TYPE, "value"),
        Input(ids.GRAPH_NODE_TYPES, "value"),
        Input(ids.GRAPH_DEPTH, "value"),
        Input(ids.GRAPH_LIMIT, "value"),
    )
    @guarded(4, fallback=([], no_update, "", []))
    def draw(
        version: dict[str, Any] | None,
        uid: str | None,
        type_name: str | None,
        node_types: list[str] | None,
        depth: int | None,
        limit: int | None,
    ) -> Any:
        if not version or (not uid and not type_name):
            raise PreventUpdate
        schema = backend.schema(version["id"])
        bound = max(1, min(int(limit or NODE_LIMIT), NODE_LIMIT_MAX))
        hops = int(depth or 1)
        notes: list[Any] = []
        if uid:
            seeds = [uid]
            origin = f"around {uid}"
        else:
            if not schema.has(type_name):
                raise PreventUpdate
            assert type_name is not None
            listing = backend.client.get(
                "/objects/{type}",
                path={"type": type_name},
                params={"at": version["id"], "columns": "uid", "limit": min(bound, SEEDS_MAX)},
            )
            seeds = [row["uid"] for row in listing.get("data", [])]
            origin = f"around the {type_name} objects"
            if listing.get("next_cursor"):
                notes.append(
                    note(
                        f"Only the first {plural(len(seeds), 'object')} of type {type_name} "
                        "(in uid order) are used as starting points.",
                        warn=True,
                    )
                )
            if not seeds:
                return (
                    [],
                    stylesheet(schema),
                    [note(f"No {type_name} objects exist at version #{version['seq']}.")],
                    [],
                )
        body: dict[str, Any] = {
            "seeds": seeds,
            "depth": hops,
            "direction": "both",
            "limit": bound,
        }
        if node_types:
            body["node_types"] = node_types
        document = backend.client.post("/graph/subgraph", params={"at": version["id"]}, body=body)
        found, edges = elements(document, set(seeds))
        nodes = len(document.get("nodes", []))
        status: list[Any] = [
            html.Div(
                f"{plural(nodes, 'node')}, {plural(edges, 'edge')} {origin}, "
                f"{hops} {'hop' if hops == 1 else 'hops'} deep, at #{version['seq']}"
            )
        ]
        if document.get("truncated"):
            status.append(
                note(
                    f"Limit of {bound} nodes reached: more nodes are within {hops} "
                    f"{'hop' if hops == 1 else 'hops'}. Narrow the types or the depth, or "
                    "raise the limit.",
                    warn=True,
                )
            )
        present = {node.get("type") for node in document.get("nodes", [])}
        return found, stylesheet(schema), status + notes, legend(schema, present)

    @app.callback(
        Output(ids.GRAPH_DETAILS, "children"),
        Output(ids.GRAPH_DETAILS_FEEDBACK, "children"),
        Input(ids.GRAPH_VIEW, "tapNodeData"),
        Input(ids.GRAPH_VIEW, "tapEdgeData"),
        Input(ids.VERSION, "data"),
    )
    @guarded(1, fallback=(None,))
    def inspect(
        node: dict[str, Any] | None, edge: dict[str, Any] | None, version: dict[str, Any] | None
    ) -> Any:
        if not version:
            raise PreventUpdate
        chosen = _latest(node, edge, ctx.triggered_id == ids.GRAPH_VIEW and ctx.triggered_prop_ids)
        if chosen is None:
            return html.Div(
                "Click a node or an edge to see its values and their provenance.", className="muted"
            )
        kind, data = chosen
        schema = backend.schema(version["id"])
        at = {"at": version["id"], "include": "provenance"}
        if kind == "node":
            found = backend.client.get("/object/{uid}", path={"uid": data["id"]}, params=at)["data"]
            return details(schema, found, version, heading=[found["type"], " ", data["id"]])
        heading = [data["edge_type"], ": ", data["source"], " → ", data["target"]]
        if data.get("link_uid"):
            found = backend.client.get("/object/{uid}", path={"uid": data["link_uid"]}, params=at)[
                "data"
            ]
            return details(schema, found, version, heading=heading)
        # A reference: the value of the holder's property names the other end.
        holder = backend.client.get("/object/{uid}", path={"uid": data["source"]}, params=at)[
            "data"
        ]
        prop = data["edge_type"].rsplit(".", 1)[-1]
        return details(schema, holder, version, heading=heading, only=prop)


def _latest(
    node: dict[str, Any] | None, edge: dict[str, Any] | None, triggered: Any
) -> tuple[str, dict[str, Any]] | None:
    """The element clicked last: the one just clicked, else the most recent of the two."""
    if triggered:
        if f"{ids.GRAPH_VIEW}.tapNodeData" in triggered and node:
            return "node", node
        if f"{ids.GRAPH_VIEW}.tapEdgeData" in triggered and edge:
            return "edge", edge
    candidates = [("node", node), ("edge", edge)]
    present = [(kind, data) for kind, data in candidates if data]
    if not present:
        return None
    return max(present, key=lambda item: item[1].get("timeStamp") or 0)
