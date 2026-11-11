"""Ids of the page's components, shared by the layout, the callbacks and the browser tests."""

from __future__ import annotations

# Header
HEALTH = "twin-health"
# What changes on every poll of `/meta/health`: the twin's clock and how far behind it is.
LIVE = "twin-live"
# What the sync section shows depends on: whether the twin is live, its last run, its job.
SYNC_KEY = "sync-key"
VERSION = "selected-version"
# The slider's value once the operator has paused; the selected version follows it.
SETTLED = "settled-version"
POLL = "health-poll"
TWIN_NAME = "twin-name"
TWIN_TIER = "twin-tier"
HEAD_LABEL = "head-label"
LIVE_LINE = "live-line"
SLIDER = "version-slider"
VERSION_DETAIL = "version-detail"
JUMP = "jump-to-head"
FOLLOW = "follow-head"
NEWER = "newer-versions"
HEALTH_FEEDBACK = "health-feedback"
VERSION_FEEDBACK = "version-feedback"
TABS = "tabs"
# Written by the browser after it asked grids to read their rows again; read by nothing.
GRIDS_REFRESHED = "grids-refreshed"

# Graph tab
GRAPH_OBJECT = "graph-object"
# The search field's text once the operator has paused typing.
GRAPH_SEARCH_TEXT = "graph-search-text"
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
# The version the sheet's grid was last asked to read its rows at again.
SHEET_REFRESH = "sheet-refresh"
# Role of the pattern-matching id of the grid, re-created per sheet and set of columns.
SHEET_GRID = "sheet-grid"

# History tab
FIDELITY = "fidelity"
LOG_CAUSE = "log-cause"
LOG_ACTOR = "log-actor"
LOG_FROM = "log-from"
LOG_TO = "log-to"
LOG_BOX = "log-box"
LOG_FEEDBACK = "log-feedback"
# The head the commit log ends at.
LOG_HEAD = "log-head"
# Role of the pattern-matching id of the commit log grid, re-created per filter.
COMMIT_LOG = "commit-log"
DIFF = "diff"
DIFF_FEEDBACK = "diff-feedback"
LOGS_LEVEL = "logs-level"
LOGS_CONTEXT = "logs-context"
LOGS_REFRESH = "logs-refresh"
LOGS_GRID = "logs-grid"
LOGS_FEEDBACK = "logs-feedback"

# The sync section of the history tab: the twin at its head, whatever version is selected.
SYNC_SECTION = "sync-section"
SYNC_STATIC = "sync-static"
SYNC_BODY = "sync-body"
SYNC_FEEDBACK = "sync-feedback"
# The sync key the section was last read for.
SYNC_SHOWN = "sync-shown"
RUN_NOW = "run-now"
RUN_STATUS = "run-status"
RUN_FEEDBACK = "run-feedback"
# The sync job this page asked for, and its outcome once it ended.
RUN_JOB = "run-job"
SOURCES = "sync-sources"
RUNS_STATUS = "runs-status"
RUNS_KIND = "runs-kind"
RUNS = "sync-runs"
RUNS_FEEDBACK = "sync-runs-feedback"
# Role of the pattern-matching id of the runs grid, re-created per filter.
RUNS_GRID = "sync-runs-grid"
REJECTS_RUN = "rejects-run"
REJECTS_RULE = "rejects-rule"
REJECTS_MAPPING = "rejects-mapping"
REJECTS = "rejects"
REJECTS_TOTAL = "rejects-total"
REJECTS_FEEDBACK = "rejects-feedback"
# Role of the pattern-matching id of the grid of rows set aside, re-created per run and filter.
REJECTS_GRID = "rejects-grid"
