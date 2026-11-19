# Models

A `predictive` or `prescriptive` question is answered by a forecast, and a forecast comes from a model the twin selects among candidates by backtesting them on its own history. You declare what may be tried; the twin measures and selects.

## Declare one model per forecast question

Name it after the question: `models.<question>`. Then set `contract.questions.<question>.model` to `<question>` and `contract.questions.<question>.horizon` to how far ahead the question looks, as a duration (`PT2H`).

| Field | Value |
|---|---|
| `models.<question>.question` | The question's id. |
| `models.<question>.target` | The `Type.property` predicted: one of the question's targets, numeric, with `dta_role: state`, mapped with a `valid_time`. |
| `models.<question>.candidates.<candidate>.family` | A shipped family (listed in [manifest](manifest.md), "Model families") or `module:name` of your own code. |
| `models.<question>.candidates.<candidate>.params` | Fixed settings of the family, such as `season_length`; leave out to use its defaults. |
| `models.<question>.features` | Other properties used as inputs by the families `ridge_lags` and `gbm_lags`. A forecast needs each feature's values over the coming horizon from the twin's own data, so name only properties whose future values the sources deliver ahead of time (a weather forecast, a production schedule). Otherwise leave it out. |
| `models.<question>.reads` | Only for a family of your own code, and then always: every observed property a forecast of that code reads from the twin, as `Type.property` (a simulator lists the state it starts from and what drives it, not what it was fitted on); `[]` when it reads none besides its target's own history through `features`. When a sensor is suspect, the twin uses this to keep its own estimate of the value fresh without ever feeding a model its own estimates. Left out, the twin takes the code to read everything: `dta manifest validate` warns with `model.reads_undeclared`. |
| `models.<question>.backtest.initial_window` | History used before the first forecast. |
| `models.<question>.backtest.step` | Time between two forecast origins. |
| `models.<question>.backtest.min_folds` | Fewest origins for a valid backtest; default 3. |
| `models.<question>.acceptance.metric` | The question's `tolerance.metric`. |
| `models.<question>.acceptance.threshold` | The largest acceptable backtest error: the question's `tolerance.value` or less. |
| `models.<question>.acceptance.baseline` | Optional: `persistence` or `seasonal`; a candidate must also beat that naive model. |
| `models.<question>.interventional` | `true` only for a model valid for what-if changes to controls; default `false`. |

## Default candidates

Propose, for a target with history:

```yaml
candidates:
  persistence:
    family: persistence
  seasonal:
    family: seasonal_naive
  ets:
    family: ets
```

When none of these three meets the tolerance in the backtest, add `ridge_lags` as a fourth candidate and build again from `models`; give it `features` only as the row above allows. The families are ordered from the simplest; among equally good candidates the simpler one is selected.

`season_length`, where a family takes it, is a number of time steps (below): the length of the cycle the series repeats. Its default is one day, `round(P1D / sync.interval)` steps: 96 for `PT15M`, 24 for `PT1H`. A family with a season needs two whole seasons of history before its first forecast origin (two days with the default).

## The time step

Every model works on a grid of `sync.interval`, in a static twin as in a live one: the history is read as one value per interval, and a forecast predicts one value per interval. So:

- a static twin with a forecast question needs `sync.interval` too (`grid.no_interval`), derived like a live twin's: `dta sync derive --write` ([live twins](live.md), "The sync interval"), never by hand;
- each forecast question's `horizon` is a whole multiple of `sync.interval` (`grid.horizon`); the derivation fits the interval to the horizons, so derive after the horizons are written;
- `backtest.step` is a whole multiple of `sync.interval` (`grid.backtest_step`).

## Size the backtest from the history

The profile gives the history span `H` of the target's dataset: `history_end` minus `history_start` of `sources.<source>.profile.datasets.<dataset>`. The backtest fits on everything before its first forecast origin (`initial_window`), then forecasts the horizon from an origin every `step` until the end of the history: each origin is one fold. The check `feasible.history` refuses a model whose history is shorter than `initial_window + min_folds * step`, unless the question is provisional.

The rule, for a horizon `h` and a history span `H`:

1. `step`: `h` (a whole multiple of `sync.interval`, as the derivation makes it). Origins then fall at every horizon, at every hour of the day for a 1 h horizon.
2. The span scored, `T`: `H / 3`, rounded down to whole days, at least `P1D` (so the origins cover every hour of a day) and at most `P7D` (every day of a week); and at least `3 * step`.
3. `initial_window`: `H - T`.
4. `min_folds`: 3.

The folds are `(T - h) / step + 1`. Examples, with `PT15M` steps of the grid and `h = PT1H`: `H = P21D` gives `T = P7D`, `initial_window: P14D`, `step: PT1H` and 168 folds; `H = P14D` gives `T = P4D`, `initial_window: P10D` and 96 folds. A backtest refits each candidate only as often as its time budget allows, so many folds cost forecasts, not fits.

If `initial_window` comes out shorter than two days (two seasons with the default season) or than `h` times four, the history is too short: tell the user the question can only be provisional until more history exists, and with their agreement set `contract.questions.<question>.provisional: true`. A provisional question is built and answered, and every answer says it is provisional.

## Acceptance

`acceptance.threshold` above the question's tolerance on the same metric draws the warning `feasible.acceptance`: the twin would accept a model that cannot meet the contract. Keep it at or below the tolerance.

## What happens at build

The build's `models` step backtests every candidate and selects the best accepted one. A specification with no accepted candidate does not fail the build: the question then stays provisional and the acceptance check `models.accepted` fails in stage 7. To evaluate again after a change, without a full build:

```sh
dta models evaluate --spec <question>
dta models list
```

`dta models list` shows each model's `status`, `family` and the span it was fitted on.

## Your own family

The manifest accepts `family: <module>:<name>` for code of your own in the code directory (`runtime.code`), but the interface such code must follow is not described in this skill. Use the shipped families. If none of them can meet a question's tolerance on the backtest, tell the user the best measured error against the tolerance and let them choose: a larger tolerance, a shorter horizon, or the question kept provisional.
