# Live twins, history, actions and control

A live twin (`contract.tier: live`) reads its sources on a schedule, writes a new version each time, measures its own error and raises failures. This file also covers how any twin loads history, and how a twin proposes or takes actions.

## Loading history

The build loads the history the sources already hold. `sync.backfill_interval` is the width of the versions it is loaded in: without it, all of history becomes one version and the twin cannot show how anything changed (the acceptance check `readiness.R4` fails). For every twin, static or live, that has a mapping with a `valid_time`, set `sync.backfill_interval` to the `refresh_period` the profile measured for that dataset, rounded up to a whole minute, hour or day.

## The sync interval

Never write `sync.interval` by hand: it is derived from the measurements and the contract by

```sh
dta sync derive --write
```

The rule is `max(source_period, rate_period, min(freshness_bound, change_time / 2))`: no faster than the sources deliver or allow, and fast enough that every question stays within its staleness and every signal is seen before it changes. The inputs it used are kept in `sync.derivation`. Outcomes are in [stage 4](stage-4-design.md), step 7.

Run it again after any change to a question's staleness or to a source, and before `dta manifest validate`. If it fails with `sync.interval_confirmed`, someone wrote the interval by hand: delete `interval` under `sync` and run it again.

Optional sync settings: `sync.aggregation` (`last`, the default, `mean`, `min` or `max`: how a signal faster than the interval is reduced to one value per version), `sync.minimum_interval` and `sync.maximum_interval` (limits for the twin's own corrections).

## Fidelity

A live twin checks itself every `fidelity.interval`: it scores each forecast question's past forecasts against what was then observed, looks for stuck or drifting signals, and opens an incident when a question stays outside its tolerance. Both fields are required for a live twin.

- `fidelity.interval`: the sync interval rounded up to a whole hour, and at least `PT1H`.
- `fidelity.window`: the history each check measures over. At least the longest forecast horizon plus five fidelity intervals (a breach needs three scored forecasts inside the window), at least `P1D`, and no longer than the history the sources held when profiled.

`fidelity.correction` sets what the twin may try by itself before it asks for help; keep its defaults (two re-fits, re-selection among candidates, then a request to you to re-model, with a budget of three cycles and 24 hours to answer). [operate](operate.md) says what to do when it asks.

## Notifications

`contract.notification.kind` is the user's choice ([question bank](question-bank.md)). For `webhook`, the variable named in `contract.notification.webhook_env` must be declared under `env` with `secret: true`; the user writes the address into `.env`.

## The twin's clock

`runtime.clock` is `wall` (the default: the twin's time is this machine's). Use `source:<source>` only when the source is a simulation or a replay whose time is not the wall's; that source then needs `sources.<source>.options.clock` (an HTTP source: the endpoint and field that give its current time).

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

In the URL, headers and body, `${NAME}` is a variable listed in the executor's `env` (declare it under `env` too), `{uid}` is the uid of the object acted on, `{id}` that uid after its first colon, and `{<parameter>}` a parameter of the action. A body value that is exactly `"{value}"` is sent with the parameter's own type. A header carrying a literal credential is refused (`secret.inline`). The address and the body format come from the user or the system's documentation; never guess them, and never call the address yourself.

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
