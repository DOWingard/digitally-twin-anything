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
HEADER = "header"
# The branch selector: the live twin, or a scenario whose branch exists.
BRANCH_SELECT = "branch-select"
# The scenarios whose branch exists, by branch name, as last read.
BRANCHES = "known-branches"
# The branch every tab reads: its name and, for a scenario, its base and head.
BRANCH = "selected-branch"
# The version and following the session had on the live twin, restored on coming back.
LIVE_KEPT = "live-kept"
SCENARIO_NOTICE = "scenario-notice"
BRANCH_FEEDBACK = "branch-feedback"
# The fidelity part of the twin's health as the last poll gave it, and whether it is live.
FIDELITY_HEALTH = "fidelity-health"
# What the manifest at the head declares that decides which tabs a twin has.
FEATURES = "twin-features"
FEATURES_FEEDBACK = "features-feedback"
STATE_BADGE = "state-badge"
# Said under the header while the twin is failed, with the failed incidents' subjects.
FAILED_LINE = "failed-line"
FAILED_FEEDBACK = "failed-feedback"
# The fidelity health the failed line was last read for.
FAILED_SHOWN = "failed-shown"
FIDELITY_TAB = "fidelity-tab"
ACTIONS_TAB = "actions-tab"
# Drawn hidden once a twin with fidelity is known, so that the chart library is loaded
# before the fidelity tab is opened.
CHART_PRELOAD = "chart-preload"

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

# Questions tab
QUESTIONS_GRID = "questions-grid"
QUESTIONS_STATUS = "questions-status"
QUESTIONS_FEEDBACK = "questions-feedback"
# The version and branch the questions were last read at.
QUESTIONS_SHOWN = "questions-shown"
# The question chosen in the table, and the function that answers it.
QUESTION_CHOSEN = "question-chosen"
QUESTION_TITLE = "question-title"
QUESTION_FORM = "question-form"
QUESTION_FORM_FEEDBACK = "question-form-feedback"
# Role of the pattern-matching ids of the form's fields, one per argument.
QUESTION_ARG = "question-arg"
QUESTION_AT = "question-at"
QUESTION_BRANCH = "question-branch"
ASK = "ask"
# Where a refusal is shown: beside the form, which keeps what was typed.
ASK_FEEDBACK = "ask-feedback"
ANSWER = "answer"
# Role of the pattern-matching id of the grid of an answer's rows, made per answer.
ANSWER_GRID = "answer-grid"
ANSWER_CHART = "answer-chart"
ANSWER_FEEDBACK = "answer-feedback"

# Scenarios tab
SCENARIO_LIST = "scenario-list"
SCENARIO_LIST_STATUS = "scenario-list-status"
SCENARIO_LIST_FEEDBACK = "scenario-list-feedback"
# The head of the live twin the list was last read at.
SCENARIO_LIST_SHOWN = "scenario-list-shown"
# The scenario chosen in the list, as the list described it.
SCENARIO_PICKED = "scenario-picked"
SCENARIO_TITLE = "scenario-title"
COMPARE_TARGETS = "compare-targets"
COMPARE_AGAINST = "compare-against"
COMPARE = "compare"
COMPARE_FEEDBACK = "compare-feedback"
COMPARISON = "comparison"
# Roles of the pattern-matching ids made per comparison: a chart and its choice of
# objects per target, and the grid of every row.
COMPARE_CHART = "compare-chart"
COMPARE_PICK = "compare-pick"
COMPARE_NOTE = "compare-note"
COMPARE_GRID = "compare-grid"
COMPARE_GRID_FEEDBACK = "compare-grid-feedback"
COMPARE_PRELOAD = "compare-preload"

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

# Fidelity tab: the live twin at the selected version.
# The version the tab was last read at: every part reads again when it changes.
FIDELITY_SHOWN = "fidelity-shown"
FIDELITY_SUMMARY = "fidelity-summary"
FIDELITY_FEEDBACK = "fidelity-feedback"
FIDELITY_RANGE = "fidelity-range"
FIDELITY_CHARTS = "fidelity-charts"
FIDELITY_CHARTS_FEEDBACK = "fidelity-charts-feedback"
# Role of the pattern-matching id of a question's chart.
FIDELITY_CHART = "fidelity-chart"
INCIDENT_COUNTS = "incident-counts"
INCIDENTS_STATUS = "incidents-status"
INCIDENTS_GRID = "incidents-grid"
INCIDENTS_FEEDBACK = "incidents-feedback"
# The incident chosen in the grid, by uid.
INCIDENT_CHOSEN = "incident-chosen"
INCIDENT_DETAIL = "incident-detail"
INCIDENT_FEEDBACK = "incident-feedback"
SIGNALS_GRID = "signals-grid"
SIGNALS_FEEDBACK = "signals-feedback"
CHECKS_GRID = "checks-grid"
CHECKS_FEEDBACK = "checks-feedback"
NOTIFICATIONS_GRID = "notifications-grid"
NOTIFICATIONS_STATUS = "notifications-status"
NOTIFICATIONS_FEEDBACK = "notifications-feedback"

# Actions tab: the live twin at the selected version.
ACTIONS_SHOWN = "actions-shown"
ACTIONS_NOTE = "actions-note"
ACTION_TYPES_GRID = "action-types-grid"
EXECUTOR_STATUS = "executor-status"
ACTION_TYPES_FEEDBACK = "action-types-feedback"
RECORDS_STATUS = "records-status"
RECORDS_ACTION = "records-action"
RECORDS = "records"
RECORDS_FEEDBACK = "records-feedback"
# Role of the pattern-matching id of the records grid, re-created per filter.
RECORDS_GRID = "records-grid"
# The record chosen in the grid, by uid.
RECORD_CHOSEN = "record-chosen"
RECORD_DETAIL = "record-detail"
RECORD_FEEDBACK = "record-feedback"
LOOPS_GRID = "loops-grid"
LOOP_RUNS = "loop-runs"
LOOPS_FEEDBACK = "loops-feedback"
