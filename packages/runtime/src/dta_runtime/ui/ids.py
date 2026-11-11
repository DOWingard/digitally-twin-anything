"""Ids of the page's components, shared by the layout, the callbacks and the browser tests."""

from __future__ import annotations

# Header
HEALTH = "twin-health"
VERSION = "selected-version"
POLL = "health-poll"
TWIN_NAME = "twin-name"
TWIN_TIER = "twin-tier"
HEAD_LABEL = "head-label"
SLIDER = "version-slider"
VERSION_DETAIL = "version-detail"
JUMP = "jump-to-head"
FOLLOW = "follow-head"
NEWER = "newer-versions"
HEALTH_FEEDBACK = "health-feedback"
VERSION_FEEDBACK = "version-feedback"
TABS = "tabs"

# Graph tab
GRAPH_OBJECT = "graph-object"
GRAPH_TYPE = "graph-type"
GRAPH_NODE_TYPES = "graph-node-types"
GRAPH_DEPTH = "graph-depth"
GRAPH_LIMIT = "graph-limit"
GRAPH_VIEW = "graph-view"
GRAPH_STATUS = "graph-status"
GRAPH_LEGEND = "graph-legend"
GRAPH_DETAILS = "graph-details"
GRAPH_FEEDBACK = "graph-feedback"
GRAPH_SEARCH_FEEDBACK = "graph-search-feedback"
GRAPH_DETAILS_FEEDBACK = "graph-details-feedback"

# Spreadsheet tab
SHEET_TYPE = "sheet-type"
SHEET_BOX = "sheet-box"
SHEET_STATUS = "sheet-status"
SHEET_FEEDBACK = "sheet-feedback"
# Role of the pattern-matching id of the grid, re-created per sheet and version.
SHEET_GRID = "sheet-grid"

# History tab
FIDELITY = "fidelity"
LOG_CAUSE = "log-cause"
LOG_ACTOR = "log-actor"
LOG_FROM = "log-from"
LOG_TO = "log-to"
LOG_BOX = "log-box"
LOG_FEEDBACK = "log-feedback"
# Role of the pattern-matching id of the commit log grid, re-created per filter.
COMMIT_LOG = "commit-log"
DIFF = "diff"
DIFF_FEEDBACK = "diff-feedback"
RUNS = "sync-runs"
RUNS_FEEDBACK = "sync-runs-feedback"
LOGS_LEVEL = "logs-level"
LOGS_CONTEXT = "logs-context"
LOGS_REFRESH = "logs-refresh"
LOGS_GRID = "logs-grid"
LOGS_FEEDBACK = "logs-feedback"
