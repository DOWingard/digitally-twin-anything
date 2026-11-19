# Stage 4: design

Goal: everything the build needs, decided and written: the ontology, how each source's rows become objects, how each question is answered, the models behind forecasts, and for a live twin how often it syncs and checks itself. The user agrees the ontology's types; everything else here is yours.

Gate `design` is recorded by `dta manifest validate`. It passes when the manifest has no error finding, and goes stale on any later change to `manifest.yaml`. Run `dta manifest validate` last, after every change.

Work in this order. If you work with other agents, `dta tasks plan --stage 4` lists one mapping task per source and one task per question; see [delegation](delegation.md). Steps 1 and 2 are always yours.

`dta` commands rewrite `manifest.yaml` when they write to it (`dta ontology check --write`, `dta manifest confirm`, `dta sync derive --write`, `dta profile`): lines are reflowed and defaults filled in. Read the file again before each edit you make after any such command; an edit made from an older copy fails or undoes what the command wrote.

When the design names a variable that is not yet in `.env` (an executor's address or key, a webhook, a new source's password), add it under `env`, then run `dta env template` again (it adds the new names to `.env` and leaves every line already there as it is), have the user fill in the new names as [stage 2](stage-2-access.md), step 4, says, and run `dta env check`.

## 1. Write the ontology

Write one LinkML file, `<workspace>/ontology/<id>.yaml`, as [ontology](ontology.md) says: one class per kind of object the questions talk about, a reference property for each "belongs to" relation, and a property for every value a question or control names. Use exactly the `Type.property` names the contract already uses (control targets). Then set `ontology.path` to `ontology/<id>.yaml`.

```sh
dta ontology check
```

- Exit 1, code `ontology.unusable`: the file breaks a convention. `error.detail.problems` lists each with its `rule`, `class`, `slot` and `message`; fix every one (the rules are in [ontology](ontology.md)) and run it again.
- Exit 1, code `ontology.signature`: the file is usable, and its types differ from those in the manifest (always so the first time). Go to step 2. `error.detail.types` lists the object, link and abstract types.
- Exit 0: the file's types are the ones in the manifest. Go to step 3.

## 2. Agree the types with the user

The user owns the types: which kinds of objects and links the twin knows, not the details of each property.

1. Show the user the types in plain words, one line each, from `error.detail.types` of the check and your file. Example: "House: a greenhouse, with its name and floor area. Bed: a growing bed, in one house, with its crop and its soil moisture over time."
2. Ask: "Are these the things the twin should know about? Say what is missing or wrong." Change the file for each answer, run `dta ontology check` again and show the changed lines.
3. When the user says yes:

   ```sh
   dta ontology check --write
   dta manifest confirm ontology.type_signature
   ```

   The first writes the file's signature into `ontology.type_signature` (exit 0, `result.written` true); the second confirms it.

Whenever you later change the types (a class or a link type added, removed or renamed), the signature changes: run `dta ontology check --write`, show the user the change, and confirm again. Changing only properties keeps the signature.

## 3. Map the sources

For every source, write `sources.<source>.mappings` as [sources](sources.md), "Mappings", says: one mapping per dataset and type, with its identity columns, its properties, and `valid_time` for rows that carry a time. Every required property of a type must be filled by some mapping, and every reference property must be mapped, or the twin has no links. Use the profile of stage 3 for column names and keys.

## 4. Say which properties play which role

Write `contract.boundary`:

- `contract.boundary.observations`: every `Type.property` a source reports (every mapped property a question or control rests on).
- `contract.boundary.state`: the properties that make up the system's state: the observed properties that change with time, and any you derive.
- `contract.boundary.controls`: the target of every control, if there are controls.
- `contract.boundary.drivers`: outside influences the twin reads but cannot set (weather, demand), if any are mapped.

## 5. Answer each question

For each question, as [functions](functions.md) and [models](models.md) say:

1. `contract.questions.<question>.targets`: the properties the answer depends on.
2. `contract.questions.<question>.function`: the id of the function that answers it, declared under `functions`.
3. `contract.questions.<question>.example`: arguments the twin is asked with when it checks it can answer (an empty mapping `{}` when the function's fixed parameters say everything).
4. For a `predictive` or `prescriptive` question: `contract.questions.<question>.horizon`, `contract.questions.<question>.model`, the model under `models.<question>` and the function `functions.<question>` with entry `forecast`. Take the backtest `step` equal to the horizon (or a whole multiple of it): step 7 fits the time step to the horizons, and the step must be a whole multiple of it ([models](models.md), "The time step").

## 6. Controls, actions and the objective

Only when the contract has controls or a prescriptive question; see [live twins](live.md), "Actions and control loops":

- for each control, an action under `actions.<action>` and its id in `contract.controls.<control>.action`;
- `contract.objective.kpi`: the id of a function that computes the objective;
- optionally, control loops under `control_loops`.

## 7. The time step and live settings

For a live twin, and for a static twin with a `predictive` or `prescriptive` question, derive the time step `sync.interval` after the horizons are written (step 5); see [live twins](live.md), "The sync interval". Never write it by hand, in either tier.

```sh
dta sync derive --write
```

- Exit 0: `sync.interval` and `sync.derivation` are written, marked derived. `result.binding` says which term decided it; `horizon` means it was shortened from `result.before_fitting` so that it divides every horizon.
- Exit 1, code `sync.infeasible`: no interval meets every question's staleness and horizon; `error.detail.problems` says which. For a staleness: tell the user the question, its staleness and the source's delivery period; propose a longer staleness. For a horizon: propose to the user a horizon that is a whole multiple of the delivery period the message names. After the user decides, run it again.
- Exit 1, code `sync.underivable`: profiling has not measured any delivery period, and no forecast states a horizon; check stage 3.
- Exit 1, code `sync.interval_confirmed`: `sync.interval` was written by hand; delete the `interval` line under `sync` and run it again.

For a live twin, then write `fidelity.interval` and `fidelity.window`, and `sync.backfill_interval` ([live twins](live.md)).

For a static twin with timed data, write `sync.backfill_interval` ([live twins](live.md), "Loading history"); a static twin writes no `fidelity` and no `control_loops` (`tier.static_extras`).

## 8. Validate

```sh
dta manifest validate
```

- Exit 0: no error finding; gate `design` is recorded as passed. Read `result.findings` anyway: each warning has a `code`, a `path` and a `message`; fix what it points at unless troubleshooting says the warning is expected.
- Exit 1, code `validate.failed`: `error.detail.findings` lists every finding; `severity` is `error` or `warning`. Fix every error (look each `code` up in [troubleshooting](troubleshooting.md)) and validate again.

Compare your manifest with the [examples](examples.md) before you go on: the same shapes, in the same places.

## 9. Check the gate

```sh
dta status
```

Gate `design` `passed`: go to [stage 5](stage-5-gaps.md). `stale`: the manifest changed after the last validation; run `dta manifest validate` again.
