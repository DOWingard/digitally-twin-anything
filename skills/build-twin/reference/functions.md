# Functions

A function computes an answer from the twin's graph and history. Every question names one (`contract.questions.<question>.function`), and so do the objective (`contract.objective.kpi`) and every control loop (`control_loops.<loop>.policy`). Functions are declared under `functions.<function>`.

## Fields

| Field | Value |
|---|---|
| `functions.<function>.description` | What it computes, in one line. |
| `functions.<function>.kind` | `builtin` (shipped with the twin) or `registered` (code you write). |
| `functions.<function>.entry` | For `builtin`, the built-in's name; for `registered`, `module:function` inside the code directory. |
| `functions.<function>.params` | Fixed arguments, the same for every call. |
| `functions.<function>.complexity` | Its cost, written as text: `O(\|V\|)` for one pass over the objects, `O(\|V\| * versions)` over history. |
| `functions.<function>.latency_budget` | The longest acceptable run time, as a duration: `PT2S` for a lookup, `PT10S` over history, `PT60S` for a forecast. |
| `functions.<function>.trigger` | `on_request` (the default), `on_commit` (after every sync) or `interval`. |
| `functions.<function>.interval` | The period, when the trigger is `interval`. |
| `functions.<function>.env` | Variables a registered function's code may read; it sees no others. |

## Choose a built-in for each question

The built-ins and every parameter they take are listed in [manifest](manifest.md), "Built-in functions". A parameter is either fixed in `params` or given with each call; the question's `example` gives the call's arguments when the twin checks the question.

| The question asks | Built-in | Fixed `params` | `example` |
|---|---|---|---|
| The value of a property, per object, now or at a time | `latest` | none, or `property` | `{property: Bed.moisture}` when not fixed |
| Which objects are above or below a threshold | `above`, `below` | `property`, `threshold`, and `unit` if it differs | `{}` |
| How a property changed over a range of time | `series` | `property` | `{from: <version>, to: head}` |
| A total over time (energy from power, volume from flow) | `integrate` | `property`, and `period` for one value per period | `{from: <version>, to: head}` |
| A sum, mean, minimum, maximum or count over objects | `aggregate` | `property`, `op`, and `group_by` per group | `{}` |
| How two objects are connected | `paths` | `edge_types`, `weight` as needed | `{source: <uid>, target: <uid>}` |
| What a property will be | `forecast` | `question: <question>` | `{}` |

A version is `head` (the newest), `#<n>` (the n-th version), a version id, or a time in ISO 8601 (`2025-06-01T00:00:00Z`).

### Filters: `where` and `edge_where`

`latest`, `above`, `below` and `aggregate` take `where`: a list of texts, each `<property>:<op>:<value>`, over properties of the class of `property`. An object counts only if it passes every clause. `paths` takes `edge_where` in the same form with the link class in front: `Pipe.diameter:ge:0.2`.

| `op` | Meaning | Example |
|---|---|---|
| `eq`, `ne` | equal, not equal | `status:eq:open` |
| `lt`, `le`, `gt`, `ge` | below, at most, above, at least | `mean_demand:gt:0` |
| `in` | one of the values, separated by commas | `zone:in:north,south` |
| `like` | a text property matches an SQL pattern (`%` any text, `_` one character) | `name:like:PMP-%` |
| `null`, `notnull` | has no value, has a value; no value is written | `closed_at:null` |

The value is read as the property's kind: a number for a numeric property, `true` or `false`, a time with its offset (`2026-01-05T00:00:00Z`). Write `uid` as the property to filter on the object's uid. A property that holds a list cannot be filtered on. `["mean_demand > 0"]` or `{mean_demand: {gt: 0}}` are refused: `dta manifest validate` checks each clause (`ref.function_entry`, `ontology.unknown_property`, `ontology.function_param`).

### What the built-ins cannot do

| The question needs | Built-ins | What to do |
|---|---|---|
| The top N, or any order | No built-in sorts or cuts its result. | A registered function that computes the counts and returns the first N in order; or, when the user only needs the counts, `aggregate`, saying in the handover that the caller sorts. |
| Groups by two properties (pairs of stations) | `aggregate.group_by` takes one property. | Map a property that holds the pair, with an `expression` in the mapping (`start_station \|\| '>' \|\| end_station`), and group by it; or a registered function. |
| Two properties in one answer (bikes and free docks) | Each built-in reads one `property`. | A registered function that reads both (`columns="bikes,docks"`); or leave `property` out of `params` and name it in each call, with the question's `example` naming one of them. |

The twin can compare a question answered by a function with the noise of its data only when the function is a built-in whose `params` fix `property`. A registered function, or a built-in that leaves `property` to the call, is validated on latency alone (see [stage 8](stage-8-handover.md), "Gather the numbers").

Rules the checks enforce:

- `ref.function_entry`: a built-in entry must be one of the built-ins; a fixed parameter must be one it takes, in the right shape.
- `ontology.function_param`, `ontology.unknown_property`: a property parameter must exist in the ontology; `above`, `below` and `integrate` need a numeric one.
- `ref.question_function`: the function a question names must be declared.

When an example needs a uid or a version, take it from the data the profile shows (the identity values) and write it as the uid the twin makes (see [sources](sources.md), "Mappings"). Keep examples small: the acceptance stage asks every question with its example and requires the answer within the function's latency budget.

## Name forecast functions after their question

For a `predictive` or `prescriptive` question, declare `functions.<question>` with `kind: builtin`, `entry: forecast` and `params: {question: <question>}`, and set `contract.questions.<question>.function` to `<question>`. Delegated question tasks own exactly these ids ([delegation](delegation.md)).

## Registered functions

Write a registered function only when no built-in can compute the answer, and for every control loop's policy.

1. Set `runtime.code` to a directory in the workspace, such as `code`.
2. Write `<workspace>/code/<module>.py` with one function per entry:

   ```python
   def policy(twin):
       rooms = twin.get("/objects/Room", columns="temperature,setpoint")["data"]
       return {"actions": [], "notes": [f"{len(rooms)} room(s) looked at"]}
   ```

   The first argument is a read-only view of the twin at the version the call is made at: `twin.get(path, **params)` returns the JSON document of `GET /api/v1<path>`, `twin.table(path, **params)` a whole table. Every further argument is a parameter, from `params` or from the call. The function returns a JSON value: a number, a text, a list or a mapping. The paths are the twin's API paths; `GET /api/v1/openapi.json` of a built twin lists them all, and `/objects/<Type>` lists the objects of a class with the properties named in `columns`.
3. Declare it: `functions.<function>.kind: registered`, `functions.<function>.entry: <module>:<function>`.
4. If the code needs packages beyond the standard library, list them in a requirements file in the workspace and name it in `runtime.worker_requirements`; the build makes an image with them.

The check `ref.code_declared` refuses a registered function while `runtime.code` is not set. The code runs in a separate worker with the limits `runtime.worker_cpus` and `runtime.worker_memory_mb`.

## The policy of a control loop

A control loop's policy is a registered function that returns the actions it proposes now:

```python
{
    "actions": [
        {"action": "<action>", "object": "<uid>", "params": {"value": 3.0}, "reason": "<why>"}
    ],
    "notes": ["<anything worth recording>"],
}
```

Every proposed action is judged by the twin against the control's bounds and autonomy before anything is sent. See [live twins](live.md), "Actions and control loops", and the [live example](examples.md).

## The objective's function

`contract.objective.kpi` names a function whose answer is the number the objective improves, in the direction `contract.objective.direction` says. Usually a built-in `aggregate` over the controlled objects: the warmest room is `entry: aggregate`, `params: {property: Room.temperature, op: max}`.
