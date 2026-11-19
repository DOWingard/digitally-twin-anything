# Live twins, history, actions and control

A live twin (`contract.tier: live`) reads its sources on a schedule, writes a new version each time, measures its own error and raises failures. This file also covers how any twin loads history, and how a twin proposes or takes actions.

## Loading history

The build loads the history the sources already hold. `sync.backfill_interval` is the width of the versions it is loaded in: without it, all of history becomes one version and the twin cannot show how anything changed (the acceptance check `readiness.R4` fails). For every twin, static or live, that has a mapping with a `valid_time`, set `sync.backfill_interval` to the `refresh_period` the profile measured for that dataset, rounded up to a whole minute, hour or day.

## The sync interval

`sync.interval` is the twin's time step. A live twin syncs once per interval, and every twin, static or live, makes its forecasts and backtests in steps of it. A live twin needs it, and so does a static twin with a `predictive` or `prescriptive` question or a model (`grid.no_interval`).

Never write `sync.interval` by hand: it is derived from the measurements and the contract by

```sh
dta sync derive --write
```

The rule is `max(source_period, rate_period, min(freshness_bound, change_time / 2))`: no faster than the sources deliver or allow, and fast enough that every question stays within its staleness and every signal is seen before it changes. When forecast questions have horizons, the result `d` is then fitted to `g`, the largest duration that divides every horizon: `g / ceil(g / d)`, the longest interval not above `d` that divides `g`. A 1 h horizon and a rule result of 3 h 7 min 30 s give `PT1H`; 25 min gives `PT20M`; 15 min stays `PT15M`. If the fitted interval would be shorter than what the sources deliver, it fails with `sync.infeasible`: make each horizon a whole multiple of the delivery period. The inputs it used are kept in `sync.derivation`. Outcomes are in [stage 4](stage-4-design.md), step 7.

Run it again after any change to a question's staleness or horizon, or to a source, and before `dta manifest validate`. If it fails with `sync.interval_confirmed`, someone wrote the interval by hand: delete `interval` under `sync` and run it again.

Optional sync settings: `sync.aggregation` (`last`, the default, `mean`, `min` or `max`: how a signal faster than the interval is reduced to one value per version), `sync.minimum_interval` and `sync.maximum_interval` (limits for the twin's own corrections). These limits do not move the derived interval: a derived interval outside them fails with `sync.infeasible`.

## Fidelity

A live twin checks itself every `fidelity.interval`: it scores each forecast question's past forecasts against what was then observed, looks for stuck or drifting signals, and opens an incident when a question stays outside its tolerance. Both fields are required for a live twin.

- `fidelity.interval`: the sync interval rounded up to a whole hour, and at least `PT1H`.
- `fidelity.window`: the history each check measures over. At least the longest forecast horizon plus five fidelity intervals (a breach needs three scored forecasts inside the window), at least `P1D`, and no longer than the history the sources held when profiled.

`fidelity.correction` sets what the twin may try by itself before it asks for help; keep its defaults (two re-fits, re-selection among candidates, then a request to you to re-model, with a budget of three cycles and 24 hours to answer). [operate](operate.md) says what to do when it asks.

## Notifications

`contract.notification.kind` is the user's choice ([question bank](question-bank.md)). For `webhook`, the variable named in `contract.notification.webhook_env` must be declared under `env` with `secret: true`; the user writes the address into `.env`.

## The twin's clock

`runtime.clock` is `wall` (the default: the twin's time is this machine's). Use `source:<source>` only when the source is a simulation or a replay whose time is not the wall's; that source then needs `sources.<source>.options.clock` (an HTTP source: the endpoint and field that give its current time).

How to tell: after profiling, compare the newest `history_end` in `sources.<source>.profile.datasets` with today's date. A live source whose data ends days, months or years before today, while the user says it is running, keeps its own time; so does one whose documentation names a time or clock endpoint. Ask the user to confirm, then use `source:<source>`. With `wall`, such a twin measures the age of its data on this machine's clock, finds every question's data older than its staleness, and fails the acceptance check `sync.freshness`.

## Actions and control loops

An action is something the twin can do to the real system, typed and bounded. Every control of the contract needs one; a prescriptive question may use one to say what it recommends.

Declare it under `actions.<action>` and name it in `contract.controls.<control>.action`:

| Field | Value |
|---|---|
| `actions.<action>.description` | What it does to the real system. |
| `actions.<action>.control` | The control it carries out. |
| `actions.<action>.object_type` | The class of the objects it acts on (the control target's class). |
| `actions.<action>.binding` | `advisory` (the twin only records and recommends it) or `executor` (the twin calls the real system). |
| `actions.<action>.parameters.<parameter>` | Each argument: `type` (`number`, `integer`, `string`, `boolean`), and `unit`, `minimum`, `maximum`, `allowed` as they apply. Use one parameter `value` for the value the control sets. |
| `actions.<action>.settle` | How long after the call its effect must be observable; default twice the sync interval. |
| `actions.<action>.tolerance` | How far the observed value may be from the value set. |
| `actions.<action>.executor` | The call, for `binding: executor` only (below). |

Rules the checks enforce:

- `control.binding`: a control that is `supervised` or `autonomous` needs an action with `binding: executor`.
- `action.executor_addon`: `binding: executor` needs `contract.addons.executors: true`, which the user decides ([question bank](question-bank.md)).
- `tier.static_control`: a static twin's controls are `advisory`.
- `control.bounds`: each control's bounds give a minimum, a maximum or allowed values.

### The executor

```yaml
executor:
  kind: http
  url: ${COLDSTORE_CONTROL_URL}/rooms/{id}/setpoint
  method: PUT                  # POST (default), PUT or PATCH
  env:
    - COLDSTORE_CONTROL_URL
    - COLDSTORE_CONTROL_TOKEN
  headers:
    Authorization: Bearer ${COLDSTORE_CONTROL_TOKEN}
  body:
    setpoint_c: "{value}"
```

The placeholders, in the URL, the headers and the body, are all there is:

| Placeholder | Filled with |
|---|---|
| `${NAME}` | The value of a variable listed in the executor's `env` (declare it under `env` too). |
| `{uid}` | The uid of the object acted on, such as `pump:PMP-10`. |
| `{id}` | That uid after its first colon: `PMP-10`. |
| `{<parameter>}` | A parameter of the action, such as `{value}`. |

In the URL, a filled-in value is percent-encoded. A body value that is exactly `"{<parameter>}"` is sent with the parameter's own type (a number stays a number). A body value `{from: <parameter>, map: {<value>: <sent>, ...}}` sends the mapped value: `status: {from: value, map: {"on": "ON", "off": "OFF"}}`. A header carrying a literal credential is refused (`secret.inline`). The address and the body format come from the user or the system's documentation; never guess them, and never call the address yourself.

What the executor does by itself, so do not declare it:

- It sends the header `Idempotency-Key` with the id of the action's record, unique per proposed action; `dta manifest validate` refuses a template that sets `Idempotency-Key` itself (`action.reserved_header`). Do not add a parameter for a request id.
- It sends the body as JSON, waits at most 30 s, follows no redirect, and tries again twice only when no connection could be made. A call that got any answer, an error included, is never sent again.
- An answer with status 2xx is `executed`; if its JSON body holds `effective_at` (a time with an offset), that is when the change took effect, otherwise the time it was sent.

### One object, or one kind of object

An action acts on whichever object a proposal names, of the class in `actions.<action>.object_type` (or a subclass); a proposal on an object of another class is rejected. To act on one object only:

- Put `{id}` or `{uid}` in the URL, never a fixed object id: a fixed `/controls/PMP-10` would switch PMP-10 whichever pump a proposal names.
- Name the object in the control's `description` (the user signs it) and in the policy that proposes actions.
- Write `actions.<action>.precondition`: filter clauses on the object acted on, in the form `<property>:<op>:<value>` of [functions](functions.md) "Filters", joined by ` and `. For one object: `uid:eq:<uid of the object>`. The twin judges it at every proposal against the object as the head holds it; a proposal on an object that does not meet every clause is recorded as rejected with the reason `precondition` and nothing is sent. `dta manifest validate` refuses a precondition it could not evaluate (`ontology.action_precondition`).

If the user wants the twin to control only one object of a class with many, say so in the description of the control the user signs, and record it in `discussion.md`.

### Whether an action had its effect

Once an executed action's effective time plus its `settle` has passed, the twin compares the value of the control's target on the object, observed and in force at that moment, with the parameter `value` the action set. Numbers match when they differ by at most `tolerance`. Any other value matches only if it is equal and of the same type: the text `"on"` never matches the number `1`, and `true` never matches `1`. So the control's target property must be observed in the same form the action sets. If the source reports a status as `1`/`0` and the control interface takes `"on"`/`"off"`, convert one of them: in a view (`CASE value WHEN 1 THEN 'on' ELSE 'off' END`, [sources](sources.md), "Views"), or with a `{from, map}` body value that turns the number the action sets into the text the interface takes. Without an observed value in force, the record stays pending.

### Control loops

A control loop asks its policy function every `interval` which actions to propose:

| Field | Value |
|---|---|
| `control_loops.<loop>.description` | What it regulates. |
| `control_loops.<loop>.controls` | The controls it drives. |
| `control_loops.<loop>.policy` | A registered function ([functions](functions.md), "The policy of a control loop"). |
| `control_loops.<loop>.interval` | Time between two decisions; not shorter than the sync interval. |

A new loop is registered disabled. Enabling it is the user's decision, after handover: `dta control enable <loop> --by "<user's name>"`, run only when the user says so ([operate](operate.md)).

What the twin does with a proposed action depends on the control's autonomy: `advisory` records it as a recommendation; `supervised` waits for a person's approval (`dta actions approve <uid> --by "<name>"`); `autonomous` sends it. In every case it first checks the bounds and refuses what lies outside them.
