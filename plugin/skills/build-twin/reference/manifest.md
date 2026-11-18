# Manifest reference

Every field of `manifest.yaml`, as the tool's schema defines it. A path holds a placeholder in angle brackets where you choose the key: `sources.<source>` is `sources.telemetry` for a source you named `telemetry`.

- Required `yes`: the field is a gap until it has a value; `yes, when ...`: only under that condition; `no`: it may stay empty.
- Owner `user`: only the user settles it; you propose and they confirm. Owner `agent`: you settle it. `written by ...`: the tool writes it; never write it by hand.
- A required section that is missing is one gap; once it exists, each of its own required fields is.
- `field_status` maps a field path to its status (`unknown`, `proposed`, `derived`, `confirmed`); the tool writes it.
- Durations are ISO 8601: `PT30S`, `PT15M`, `PT1H`, `P1D`, `P7D`.

## manifest_version

| Field | Type | Required | Owner | Default | Description |
|---|---|---|---|---|---|
| `manifest_version` | one of `1` | no | fixed | `1` |  |

## twin

Identity of the twin.

| Field | Type | Required | Owner | Default | Description |
|---|---|---|---|---|---|
| `twin` | section | yes | agent |  | Identity of the twin |
| `twin.id` | text, matching `^[a-z][a-z0-9_-]{0,62}$` | yes | agent |  | Short name used for the stack and the skill |
| `twin.name` | text | yes | agent |  | Display name |
| `twin.revision` | integer, at least 1 | no | agent | `1` | Counts manifest revisions |

## contract

What the twin must do and within which limits.

| Field | Type | Required | Owner | Default | Description |
|---|---|---|---|---|---|
| `contract` | section | yes | user |  | What the twin must do and within which limits |
| `contract.purpose` | text | yes | user |  | What the twin is for |
| `contract.tier` | one of `static`, `live` | yes | user |  | Static (one load) or live (recurring sync) |
| `contract.cadence_preference` | duration (ISO 8601, such as `PT15M`) | no | user |  | Sync interval the user asked for; recorded only: the build does not act on it |
| `contract.questions` | map of `<question>` to a section | yes | user |  | What the twin must answer or decide |
| `contract.questions.<question>.text` | text | yes | user |  | The information or decision wanted |
| `contract.questions.<question>.kind` | one of `descriptive`, `diagnostic`, `predictive`, `prescriptive` | yes | user |  | Descriptive, diagnostic, predictive or prescriptive |
| `contract.questions.<question>.priority` | integer, at least 1 | yes | user |  | 1 is the most important |
| `contract.questions.<question>.tolerance` | section | yes | user |  | Acceptable error of the answer |
| `contract.questions.<question>.tolerance.metric` | one of `mae`, `rmse`, `mape`, `max_abs`, `error_rate` | yes | user |  | Error measure the tolerance applies to |
| `contract.questions.<question>.tolerance.value` | number, above 0 | yes | user |  | Largest acceptable error |
| `contract.questions.<question>.tolerance.unit` | text | no | user |  | Unit of the value, as a UCUM code |
| `contract.questions.<question>.staleness` | duration (ISO 8601, such as `PT15M`) | yes | user |  | Oldest data the answer may rest on |
| `contract.questions.<question>.targets` | list of text | yes | agent |  | Ontology properties the answer depends on, as Type.property |
| `contract.questions.<question>.horizon` | duration (ISO 8601, such as `PT15M`) | yes, for a predictive or prescriptive question | agent |  | How far ahead the answer looks |
| `contract.questions.<question>.function` | text | yes | agent |  | Id of the function that answers it |
| `contract.questions.<question>.example` | map of name to any value | no | agent |  | Arguments the question is asked with when the twin checks that it can answer it |
| `contract.questions.<question>.model` | text | yes, for a predictive or prescriptive question | agent |  | Id of the model spec behind it |
| `contract.questions.<question>.provisional` | true or false | no | agent | `false` | Set when history is too short to validate the answer at build |
| `contract.controls` | map of `<control>` to a section | no | user |  | Controls delegated to the twin |
| `contract.controls.<control>.description` | text | yes | user |  | What the twin is responsible for controlling |
| `contract.controls.<control>.target` | text | yes | user |  | Ontology property the control sets, as Type.property |
| `contract.controls.<control>.bounds` | section | yes | user |  | Limits the twin must stay inside |
| `contract.controls.<control>.bounds.minimum` | number | no | user |  | Lowest value the twin may set |
| `contract.controls.<control>.bounds.maximum` | number | no | user |  | Highest value the twin may set |
| `contract.controls.<control>.bounds.allowed` | list of text or number or true or false | no | user |  | Discrete values the twin may set |
| `contract.controls.<control>.bounds.max_step` | number | no | user |  | Largest change in one action |
| `contract.controls.<control>.autonomy` | one of `advisory`, `supervised`, `autonomous` | yes | user |  | Advisory, supervised or autonomous |
| `contract.controls.<control>.action` | text | yes | agent |  | Id of the action type that carries it out |
| `contract.objective` | section | yes, when a question is prescriptive or the contract has controls | user |  | What control and advice optimise |
| `contract.objective.description` | text | yes | user |  | The outcome to improve |
| `contract.objective.direction` | one of `minimize`, `maximize` | yes | user |  | Which way is better |
| `contract.objective.kpi` | text | yes | agent |  | Id of the function that computes it |
| `contract.objective.constraints` | map of `<constraint>` to a section | no | user |  | Limits on the optimisation |
| `contract.objective.constraints.<constraint>.description` | text | yes | user |  | A limit that must hold while optimising |
| `contract.objective.constraints.<constraint>.expression` | text | no | agent |  | Machine-checkable form over ontology properties |
| `contract.boundary` | section | yes | agent |  | Roles of the ontology's properties |
| `contract.boundary.state` | list of text | yes | agent |  | Properties that make up the system state |
| `contract.boundary.controls` | list of text | no | agent |  | Properties that can be set |
| `contract.boundary.drivers` | list of text | no | agent |  | Outside influences that cannot be set |
| `contract.boundary.observations` | list of text | yes | agent |  | Properties that sources report |
| `contract.notification` | section | yes, for a live twin | user |  | Where failures are raised |
| `contract.notification.kind` | one of `webhook`, `log` | yes | user |  | Where failures are raised |
| `contract.notification.webhook_env` | text | yes, when `kind` is `webhook` | user |  | Name of the variable holding the webhook address |
| `contract.addons` | section | no | user |  | Optional parts of the build |
| `contract.addons.sdk_python` | true or false | no | user | `false` | Generate a typed Python client; recorded only: the build does not act on it |
| `contract.addons.mcp` | true or false | no | user | `false` | Generate an MCP adapter from the API description; recorded only: the build does not act on it |
| `contract.addons.executors` | true or false | no | user | `false` | Let actions call external systems |
| `contract.addons.llm_extraction` | true or false | no | user | `false` | Run a language model inside the twin to re-read documents; recorded only: the build does not act on it |
| `contract.addons.readonly_sql` | true or false | no | user | `false` | Expose read-only SQL over the history; recorded only: the build does not act on it |
| `contract.hosting` | one of `local`, `cloud` | no | user | `local` | Where the twin is meant to run; recorded only: the build does not act on it |

## ontology

Domain ontology.

| Field | Type | Required | Owner | Default | Description |
|---|---|---|---|---|---|
| `ontology` | section | yes | agent |  | Domain ontology |
| `ontology.path` | text | yes | agent |  | LinkML file of the domain ontology, relative to the workspace |
| `ontology.type_signature` | text | yes | user |  | Hash of the object and link types the user confirmed |

## sources

Where the data comes from.

| Field | Type | Required | Owner | Default | Description |
|---|---|---|---|---|---|
| `sources` | map of `<source>` to a section | yes | agent |  | Where the data comes from |
| `sources.<source>.description` | text | no | user |  | What the source is |
| `sources.<source>.connector` | one of `file`, `sql`, `http`, `custom` | yes | agent |  | How the source is read |
| `sources.<source>.location` | text | yes | agent |  | Address with ${VARIABLE} placeholders; never a secret |
| `sources.<source>.env` | list of text | no | agent |  | Names of the variables the connector needs |
| `sources.<source>.options` | map of name to any value | no | agent |  | Connector settings such as format or paging |
| `sources.<source>.refresh_period` | duration (ISO 8601, such as `PT15M`) | yes, for a source of a live twin that has a mapping with a `valid_time` | agent |  | How often the source hands over something new |
| `sources.<source>.rate_limit_period` | duration (ISO 8601, such as `PT15M`) | no | agent |  | Shortest allowed gap between reads |
| `sources.<source>.edits_past` | true or false | no | agent | `false` | The source rewrites its own history |
| `sources.<source>.host_local` | true or false | no | agent | `false` | A changing file on the user's machine |
| `sources.<source>.views` | map of name to text | no | agent |  | Named SELECT statements over the datasets read in one run |
| `sources.<source>.incremental` | map of name to text | no | agent |  | Datasets read on from where the last run stopped, each with the column holding the time of its rows. A view over one sees only the new rows, so each of its rows must depend only on rows of the dataset with the same time |
| `sources.<source>.profile` | section | yes | agent |  | Result of read-only profiling; written by `dta profile` |
| `sources.<source>.profile.profiled_at` | time (ISO 8601) | no | written by `dta profile` |  |  |
| `sources.<source>.profile.datasets` | map of `<dataset>` to a section | no | written by `dta profile` |  |  |
| `sources.<source>.profile.datasets.<dataset>.rows` | integer, at least 0 | no | written by `dta profile` |  |  |
| `sources.<source>.profile.datasets.<dataset>.columns` | map of `<column>` to a section | no | written by `dta profile` |  |  |
| `sources.<source>.profile.datasets.<dataset>.columns.<column>.dtype` | text | no | written by `dta profile` |  |  |
| `sources.<source>.profile.datasets.<dataset>.columns.<column>.null_fraction` | number, at least 0, at most 1 | no | written by `dta profile` |  |  |
| `sources.<source>.profile.datasets.<dataset>.columns.<column>.distinct_count` | integer, at least 0 | no | written by `dta profile` |  |  |
| `sources.<source>.profile.datasets.<dataset>.columns.<column>.unique` | true or false | no | written by `dta profile` | `false` |  |
| `sources.<source>.profile.datasets.<dataset>.columns.<column>.minimum` | number | no | written by `dta profile` |  |  |
| `sources.<source>.profile.datasets.<dataset>.columns.<column>.maximum` | number | no | written by `dta profile` |  |  |
| `sources.<source>.profile.datasets.<dataset>.columns.<column>.noise_sigma` | number, at least 0 | no | written by `dta profile` |  | Noise floor of the signal |
| `sources.<source>.profile.datasets.<dataset>.columns.<column>.change_time` | duration (ISO 8601, such as `PT15M`) | no | written by `dta profile` |  | Characteristic time of change |
| `sources.<source>.profile.datasets.<dataset>.candidate_keys` | list of list of text | no | written by `dta profile` |  |  |
| `sources.<source>.profile.datasets.<dataset>.timestamp_columns` | list of text | no | written by `dta profile` |  |  |
| `sources.<source>.profile.datasets.<dataset>.time_column` | text | no | written by `dta profile` |  | The timestamp column the history is ordered by |
| `sources.<source>.profile.datasets.<dataset>.entity_columns` | list of text | no | written by `dta profile` |  | Columns that, with the time column, identify one row of a time series |
| `sources.<source>.profile.datasets.<dataset>.history_start` | time (ISO 8601) | no | written by `dta profile` |  |  |
| `sources.<source>.profile.datasets.<dataset>.history_end` | time (ISO 8601) | no | written by `dta profile` |  |  |
| `sources.<source>.profile.datasets.<dataset>.refresh_period` | duration (ISO 8601, such as `PT15M`) | no | written by `dta profile` |  | Median gap between consecutive timestamps |
| `sources.<source>.profile.datasets.<dataset>.sampled_rows` | integer, at least 0 | no | written by `dta profile` |  | Rows the statistics were computed on, when not all |
| `sources.<source>.profile.refresh_period` | duration (ISO 8601, such as `PT15M`) | no | written by `dta profile` |  |  |
| `sources.<source>.profile.rate_limit_period` | duration (ISO 8601, such as `PT15M`) | no | written by `dta profile` |  |  |
| `sources.<source>.mappings` | map of `<mapping>` to a section | yes | agent |  | How rows become ontology objects |
| `sources.<source>.mappings.<mapping>.dataset` | text | yes | agent |  | Table, file pattern or endpoint inside the source |
| `sources.<source>.mappings.<mapping>.object_type` | text | yes | agent |  | Ontology object or link type the rows become |
| `sources.<source>.mappings.<mapping>.identity` | list of text | yes | agent |  | Columns that identify one object |
| `sources.<source>.mappings.<mapping>.properties` | map of `<property>` to a section | yes | agent |  | Ontology property to source column |
| `sources.<source>.mappings.<mapping>.properties.<property>.column` | text | no | agent |  | Source column the value is read from |
| `sources.<source>.mappings.<mapping>.properties.<property>.expression` | text | no | agent |  | Transformation applied when one column is not enough |
| `sources.<source>.mappings.<mapping>.properties.<property>.unit` | text | no | agent |  | Unit in the source, as a UCUM code |
| `sources.<source>.mappings.<mapping>.valid_time` | text | no | agent |  | Column carrying when the fact was true; else time of read |
| `sources.<source>.mappings.<mapping>.kind` | one of `observed`, `inferred`, `assumed`, `simulated` | no | agent | `observed` | How the facts were obtained |
| `sources.<source>.mappings.<mapping>.mode` | one of `upsert`, `snapshot` | no | agent | `upsert` | upsert keeps an object that is missing from a read; snapshot deletes it |
| `sources.<source>.mappings.<mapping>.filter` | text | no | agent |  | Condition a row must meet to be mapped |
| `sources.<source>.mappings.<mapping>.cursor` | text | no | agent |  | Column that only grows, so a later read can take only new rows |

## sync

Recurring sync.

| Field | Type | Required | Owner | Default | Description |
|---|---|---|---|---|---|
| `sync` | section | yes, for a live twin | agent |  | Recurring sync |
| `sync.interval` | duration (ISO 8601, such as `PT15M`) | yes, for a live twin | agent |  | Time between sync runs |
| `sync.minimum_interval` | duration (ISO 8601, such as `PT15M`) | no | agent |  | Shortest interval correction may choose |
| `sync.maximum_interval` | duration (ISO 8601, such as `PT15M`) | no | agent |  | Longest interval correction may choose |
| `sync.derivation` | section | no | agent |  | How the interval was derived; written by `dta sync derive --write` |
| `sync.derivation.source_period` | duration (ISO 8601, such as `PT15M`) | no | written by `dta sync derive --write` |  |  |
| `sync.derivation.rate_period` | duration (ISO 8601, such as `PT15M`) | no | written by `dta sync derive --write` |  |  |
| `sync.derivation.min_staleness` | duration (ISO 8601, such as `PT15M`) | no | written by `dta sync derive --write` |  |  |
| `sync.derivation.freshness_bound` | duration (ISO 8601, such as `PT15M`) | no | written by `dta sync derive --write` |  |  |
| `sync.derivation.change_time` | duration (ISO 8601, such as `PT15M`) | no | written by `dta sync derive --write` |  |  |
| `sync.derivation.rule` | text | no | written by `dta sync derive --write` | `max(source_period, rate_period, min(freshness_bound, change_time / 2))` |  |
| `sync.aggregation` | one of `last`, `mean`, `min`, `max` | no | agent | `last` | How signals faster than the interval are reduced before commit |
| `sync.backfill_interval` | duration (ISO 8601, such as `PT15M`) | no | agent |  | Width of the versions in which existing history is loaded |
| `sync.assimilation` | text | no | agent | `direct` | How observations are merged into state |

## models

Predictive models.

| Field | Type | Required | Owner | Default | Description |
|---|---|---|---|---|---|
| `models` | map of `<model>` to a section | no | agent |  | Predictive models |
| `models.<model>.question` | text | yes | agent |  | Question the model serves |
| `models.<model>.target` | text | yes | agent |  | Ontology property predicted, as Type.property |
| `models.<model>.candidates` | map of `<candidate>` to a section | yes | agent |  | Families the twin may select among |
| `models.<model>.candidates.<candidate>.family` | text | yes | agent |  | Model family |
| `models.<model>.candidates.<candidate>.params` | map of name to any value | no | agent |  | Fixed settings of the family |
| `models.<model>.features` | list of text | no | agent |  | Ontology properties used as inputs |
| `models.<model>.backtest` | section | yes | agent |  | How candidates are evaluated |
| `models.<model>.backtest.scheme` | one of `walk_forward` | no | agent | `walk_forward` | Evaluation scheme |
| `models.<model>.backtest.initial_window` | duration (ISO 8601, such as `PT15M`) | yes | agent |  | History used before the first forecast |
| `models.<model>.backtest.step` | duration (ISO 8601, such as `PT15M`) | yes | agent |  | Time between forecast origins |
| `models.<model>.backtest.min_folds` | integer, at least 1 | no | agent | `3` | Fewest origins for a valid backtest |
| `models.<model>.acceptance` | section | yes | agent |  | When a candidate is good enough |
| `models.<model>.acceptance.metric` | one of `mae`, `rmse`, `mape`, `max_abs`, `error_rate` | yes | agent |  | Error measure used to accept a model |
| `models.<model>.acceptance.threshold` | number, above 0 | yes | agent |  | Largest acceptable backtest error |
| `models.<model>.acceptance.baseline` | one of `persistence`, `seasonal` | no | agent |  | Naive model to beat |
| `models.<model>.interventional` | true or false | no | agent | `false` | Valid for what-if changes to controls |

## functions

Computations that answer questions.

| Field | Type | Required | Owner | Default | Description |
|---|---|---|---|---|---|
| `functions` | map of `<function>` to a section | yes | agent |  | Computations that answer questions |
| `functions.<function>.description` | text | no | agent |  | What it computes |
| `functions.<function>.kind` | one of `builtin`, `registered` | yes | agent |  | Shipped with the runtime or supplied as code |
| `functions.<function>.entry` | text | yes | agent |  | Built-in name, or module:function inside the code artifact |
| `functions.<function>.params` | map of name to any value | no | agent |  | Fixed arguments |
| `functions.<function>.complexity` | text | yes | agent |  | Cost in objects, links and versions, such as O(\|E\|) |
| `functions.<function>.latency_budget` | duration (ISO 8601, such as `PT15M`) | yes | agent |  | Longest acceptable run time |
| `functions.<function>.trigger` | one of `on_request`, `on_commit`, `interval` | no | agent | `on_request` | When it runs |
| `functions.<function>.interval` | duration (ISO 8601, such as `PT15M`) | no | agent |  | Period when the trigger is interval |
| `functions.<function>.env` | list of text | no | agent |  | Names of the variables the function's code may read; it sees no other |

## actions

Typed actions on the real system.

| Field | Type | Required | Owner | Default | Description |
|---|---|---|---|---|---|
| `actions` | map of `<action>` to a section | no | agent |  | Typed actions on the real system |
| `actions.<action>.description` | text | no | agent |  | What the action does to the real system |
| `actions.<action>.control` | text | no | agent |  | Delegated control it carries out |
| `actions.<action>.object_type` | text | no | agent |  | Ontology type the action is applied to |
| `actions.<action>.binding` | one of `advisory`, `executor` | yes | agent |  | Recommendation only, or an external call |
| `actions.<action>.executor` | section | yes, when `binding` is `executor` | agent |  | The external call |
| `actions.<action>.executor.kind` | one of `http` | yes | agent |  | How the external system is called |
| `actions.<action>.executor.url` | text | yes | agent |  | Address with ${VARIABLE} placeholders; never a secret |
| `actions.<action>.executor.method` | one of `POST`, `PUT`, `PATCH` | no | agent | `POST` | HTTP method |
| `actions.<action>.executor.env` | list of text | no | agent |  | Names of the variables the call needs |
| `actions.<action>.executor.body` | map of name to any value | no | agent |  | Request body template over the action's parameters |
| `actions.<action>.executor.headers` | map of name to text | no | agent |  | Header templates; values may reference variables |
| `actions.<action>.parameters` | map of `<parameter>` to a section | no | agent |  | Arguments of the action |
| `actions.<action>.parameters.<parameter>.type` | one of `number`, `integer`, `string`, `boolean` | yes | agent |  | Value type |
| `actions.<action>.parameters.<parameter>.unit` | text | no | agent |  | Unit, as a UCUM code |
| `actions.<action>.parameters.<parameter>.minimum` | number | no | agent |  | Lowest accepted value |
| `actions.<action>.parameters.<parameter>.maximum` | number | no | agent |  | Highest accepted value |
| `actions.<action>.parameters.<parameter>.allowed` | list of text or number or true or false | no | agent |  | Accepted values |
| `actions.<action>.precondition` | text | no | agent |  | Condition on the graph that must hold |
| `actions.<action>.expected_effect` | text | no | agent |  | What should be observed afterwards |
| `actions.<action>.settle` | duration (ISO 8601, such as `PT15M`) | no | agent |  | Time after which the effect must be observable; twice the sync interval if absent |
| `actions.<action>.tolerance` | number, at least 0 | no | agent | `0.0` | How far the observed value may be from the one set |

## control_loops

Loops that drive delegated controls.

| Field | Type | Required | Owner | Default | Description |
|---|---|---|---|---|---|
| `control_loops` | map of `<loop>` to a section | no | agent |  | Loops that drive delegated controls |
| `control_loops.<loop>.description` | text | no | agent |  | What the loop regulates |
| `control_loops.<loop>.controls` | list of text | yes | agent |  | Delegated controls it drives |
| `control_loops.<loop>.policy` | text | yes | agent |  | Id of the function that chooses the action |
| `control_loops.<loop>.interval` | duration (ISO 8601, such as `PT15M`) | yes | agent |  | Time between decisions |

## fidelity

How error is measured and corrected.

| Field | Type | Required | Owner | Default | Description |
|---|---|---|---|---|---|
| `fidelity` | section | yes, for a live twin | agent |  | How error is measured and corrected |
| `fidelity.interval` | duration (ISO 8601, such as `PT15M`) | yes, for a live twin | agent |  | Time between fidelity checks |
| `fidelity.window` | duration (ISO 8601, such as `PT15M`) | yes, for a live twin | agent |  | History each check measures error over |
| `fidelity.correction` | section | no | agent |  | Limits on self-correction |
| `fidelity.correction.recalibrate_attempts` | integer, at least 0 | no | agent | `2` | Tries of re-fitting parameters before escalating |
| `fidelity.correction.reselect` | true or false | no | agent | `true` | May switch among the declared candidates |
| `fidelity.correction.agent_remodel` | true or false | no | agent | `true` | May ask the managing agent to re-model |
| `fidelity.correction.retry_budget` | integer, at least 1 | no | agent | `3` | Correction cycles before the twin is declared failed |
| `fidelity.correction.remodel_timeout` | duration (ISO 8601, such as `PT15M`) | no | agent | `P1D` | How long a request to re-model may stay unanswered before the twin is declared failed |

## interfaces

API and UI settings.

| Field | Type | Required | Owner | Default | Description |
|---|---|---|---|---|---|
| `interfaces` | section | no | agent |  | API and UI settings |
| `interfaces.base_url_env` | text | no | agent | `DTA_BASE_URL` | Variable holding the API address |
| `interfaces.token_env` | text | no | agent | `DTA_API_TOKEN` | Variable holding the API token |
| `interfaces.api_port_env` | text | no | agent | `DTA_API_PORT` | Variable holding the API port on the host |
| `interfaces.ui_port_env` | text | no | agent | `DTA_UI_PORT` | Variable holding the UI port on the host |
| `interfaces.ui` | true or false | no | agent | `true` | Serve the browser UI |

## env

Every variable the twin needs, by name.

| Field | Type | Required | Owner | Default | Description |
|---|---|---|---|---|---|
| `env` | map of `<NAME>` to a section | no | agent |  | Every variable the twin needs, by name |
| `env.<NAME>.description` | text | yes | agent |  | What the variable is for |
| `env.<NAME>.secret` | true or false | no | agent | `true` | Value must never be shown |

## runtime

Container resources.

| Field | Type | Required | Owner | Default | Description |
|---|---|---|---|---|---|
| `runtime` | section | no | agent |  | Container resources |
| `runtime.store_image` | text | no | agent | `dolthub/dolt-sql-server:2.7.3` | Image of the versioned store |
| `runtime.image` | text | no | agent | `dta-runtime:0.1.0` | Image the twin's own services run from |
| `runtime.clock` | text, matching `^(wall\|source:[a-z][a-z0-9_-]*)$` | no | agent | `wall` | Where the twin reads the current time: wall, or source:<source id> |
| `runtime.networks` | list of text | no | agent |  | Existing container networks the twin joins to reach sources running on this machine |
| `runtime.scheduler_poll` | duration (ISO 8601, such as `PT15M`), above PT0.05S, at most PT1H | no | agent | `PT1S` | Wall time between two looks of the scheduler at the twin's clock; shorter than the sync interval takes on the wall when the clock runs faster than it |
| `runtime.code` | text | no | agent |  | Directory in the workspace holding the twin's own code: registered functions and model families, named as module:name |
| `runtime.worker_requirements` | text | no | agent |  | Requirements file in the workspace, installed into the image registered code runs in |
| `runtime.worker_cpus` | number, above 0 | no | agent | `1.0` | CPU limit of the worker for registered code |
| `runtime.worker_memory_mb` | integer, at least 128 | no | agent | `2048` | Memory limit of the worker |

## Built-in functions

The entries `functions.<function>.entry` may name when `kind` is `builtin`. A parameter is fixed in `functions.<function>.params` or given with each call; a required one must be given one way or the other.

### latest

The value in force of a property, per object, at a version.

| Parameter | Kind | Required | Default | Description |
|---|---|---|---|---|
| `property` | property | yes |  | The property read |
| `uids` | uids | no |  | Only these objects; every object of the type when absent |
| `where` | filters | no |  | Only objects of the property's type that pass these filters |
| `against` | version | no |  | An earlier version: adds the value then and the change |

### below

Objects whose value in force is below a threshold: at one version, or over a range of versions with the times it was.

| Parameter | Kind | Required | Default | Description |
|---|---|---|---|---|
| `property` | property, numeric | yes |  | The property compared |
| `threshold` | number | yes |  | The bound |
| `unit` | unit | no |  | Unit of the threshold; the property's unit when absent |
| `from` | version | no |  | First version of a range; with `to` |
| `to` | version | no |  | Last version of a range; with `from` |
| `uids` | uids | no |  | Only these objects; every object of the type when absent |
| `where` | filters | no |  | Only objects of the property's type that pass these filters |

### above

Objects whose value in force is above a threshold: at one version, or over a range of versions with the times it was.

| Parameter | Kind | Required | Default | Description |
|---|---|---|---|---|
| `property` | property, numeric | yes |  | The property compared |
| `threshold` | number | yes |  | The bound |
| `unit` | unit | no |  | Unit of the threshold; the property's unit when absent |
| `from` | version | no |  | First version of a range; with `to` |
| `to` | version | no |  | Last version of a range; with `from` |
| `uids` | uids | no |  | Only these objects; every object of the type when absent |
| `where` | filters | no |  | Only objects of the property's type that pass these filters |

### integrate

The integral over time of a property, each value held from its version until the next (left step), per object and period.

| Parameter | Kind | Required | Default | Description |
|---|---|---|---|---|
| `property` | property, numeric | yes |  | The property integrated |
| `from` | version | yes |  | First version of the range |
| `to` | version | yes |  | Last version of the range |
| `period` | duration | no |  | One value per object and period, periods counted from the Unix epoch in UTC; one value per object when absent |
| `unit` | unit | no |  | Unit of the result; the property's unit times seconds if absent |
| `uids` | uids | no |  | Only these objects; every object of the type when absent |

### paths

Paths between two objects with the fewest links, or the least total weight.

| Parameter | Kind | Required | Default | Description |
|---|---|---|---|---|
| `source` | uid | yes |  | Where the paths start |
| `target` | uid | yes |  | Where the paths end |
| `edge_types` | types | no |  | Link types followed; every link type when absent |
| `edge_where` | edge_filters | no |  | Only links that pass these filters are followed |
| `weight` | name, numeric | no |  | Property of the links summed along a path |
| `default_weight` | number | no |  | Weight of a link that holds none |
| `direction` | choice: `both`, `out`, `in` | no | `both` | Way links are followed |
| `k` | integer | no | `1` | Most paths returned |
| `max_depth` | integer | no |  | Most links in a path |

### aggregate

One number over the objects of a type, or one per value of another of its properties.

| Parameter | Kind | Required | Default | Description |
|---|---|---|---|---|
| `property` | property | yes |  | The property aggregated |
| `op` | choice: `sum`, `mean`, `min`, `max`, `count` | yes |  | How its values are combined |
| `group_by` | name | no |  | A property of the same type: one result per value of it, which for a reference is one per object referred to |
| `uids` | uids | no |  | Only these objects; every object of the type when absent |
| `where` | filters | no |  | Only objects of the property's type that pass these filters |

### series

The history of a property over a range of versions, per object.

| Parameter | Kind | Required | Default | Description |
|---|---|---|---|---|
| `property` | property | yes |  | The property read |
| `from` | version | yes |  | First version of the range |
| `to` | version | yes |  | Last version of the range |
| `uids` | uids | no |  | Only these objects; every object of the type when absent |
| `step` | duration | no |  | The value in force on a regular grid of this step; on the versions of the range when absent |

### forecast

What the active model of a question predicts from a version on.

| Parameter | Kind | Required | Default | Description |
|---|---|---|---|---|
| `question` | question | yes |  | The question whose model predicts |
| `horizon` | duration | no |  | How far ahead; the question's horizon when absent |
| `uids` | uids | no |  | Only these objects; every object of the type when absent |
| `levels` | numbers | no | `[80, 95]` | Coverage of the intervals in percent |

## Model families

What `models.<model>.candidates.<candidate>.family` may name, simplest first; among equally good candidates the simpler one is selected. A family marked as a baseline is what `models.<model>.acceptance.baseline` names.

| Family | Baseline | Description |
|---|---|---|
| `persistence` | `persistence` | The last value seen, carried forward |
| `seasonal_naive` | `seasonal` | The value one season earlier |
| `theta` |  | Theta method with the season removed and put back |
| `ets` |  | Exponential smoothing, its form chosen by information criterion |
| `arima` |  | ARIMA, its orders chosen by information criterion |
| `mstl` |  | Trend and several seasons separated, the trend forecast |
| `ridge_lags` |  | Linear regression on lags of the target and the calendar |
| `gbm_lags` |  | Gradient-boosted trees on lags of the target and the calendar |
