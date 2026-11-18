# Stage 1: contract

Goal: the contract written and agreed: the tier, the questions the twin must answer, and, where they apply, the controls it takes over, the objective it serves and where it raises failures.

Gate `contract` is computed from the manifest. It passes when every required user-owned field under `contract` has a value and is confirmed, except each question's tolerance and staleness: those wait for profiling, which measures the noise and the delivery period they must respect. You ask for them now anyway, write them, and confirm them in stage 5.

The user owns every field of this stage. You propose; the user decides.

## 1. Ask, in this order

Use the [question bank](question-bank.md): it gives, per field, the question to ask, the default to propose and what to write. Ask one question at a time. After each answer, write the field into `manifest.yaml` at once and append the entry to `discussion.md`.

1. `contract.tier`: `static` or `live`.
2. The questions, one at a time, until the user has no more. For each, choose an id (`q_` and a short word: `q_moisture`), then ask for `text`, `kind`, `priority`, the tolerance (`metric`, `value`, `unit`) and `staleness`, each under `contract.questions.<question>`.
3. Only if the user wants the twin to set something in the real system: the controls, under `contract.controls.<control>`: `description`, `target`, `bounds`, `autonomy`.
4. Only if a question is `prescriptive` or a control exists: the objective, `contract.objective.description` and `contract.objective.direction`, and any constraints under `contract.objective.constraints.<constraint>.description`.
5. Only if the tier is `live`: `contract.notification.kind`, and `contract.notification.webhook_env` when the kind is `webhook`.
6. Only if a control's autonomy is `supervised` or `autonomous`: `contract.addons.executors`. The other optional fields are not asked; the [question bank](question-bank.md), "Optional contract fields", says when to write them.

Propose questions from the purpose: for a purpose like "show the moisture of each bed and which beds ran dry", propose one descriptive question ("What was the moisture of each bed at a given time?") and one diagnostic one ("Which beds were below 20 %?"). Each question must be answerable from data the sources hold; if it is not, say which data is missing and ask whether a source for it exists.

Tell the user, before they choose a kind:

- A `predictive` or `prescriptive` question needs a forecast model, and a model needs history: at least the backtest's initial window plus three steps of it (see [models](models.md)). With too little history the question stays provisional.
- A `static` twin can only advise: a control on a static twin must have `autonomy: advisory`.
- A control whose autonomy is `supervised` or `autonomous` makes the twin call the real system; that needs `contract.addons.executors: true` and a control interface the twin can call.

## 2. Leave to the agent what the agent owns

Do not ask for these; you set them in stage 4: `contract.questions.<question>.targets`, `contract.questions.<question>.function`, `contract.questions.<question>.horizon`, `contract.questions.<question>.model`, `contract.questions.<question>.example`, `contract.controls.<control>.action`, `contract.objective.kpi`, `contract.boundary` and its lists.

## 3. Show the contract and confirm what the user agreed

1. Show the user what you wrote, field by field, as a short list in plain words (for example: "q_moisture: What was the soil moisture of each bed at a given time? Descriptive, priority 1, mean absolute error at most 2 %, data no older than one day.").
2. Ask: "Is this what the twin must do? Say which items to change." Change what the user names, then show the changed items again.
3. When the user says yes, confirm exactly the fields shown, except the tolerance and staleness fields, listing each path:

   ```sh
   dta manifest confirm contract.tier contract.purpose contract.questions.<question>.text contract.questions.<question>.kind contract.questions.<question>.priority
   ```

   Exit 0: `result.confirmed` lists the paths now confirmed. Exit 2, code `confirm.refused`: `error.detail.refused` lists each path with its own code; nothing was changed. `confirm.no_value`: the field has no value yet, so write it first. `confirm.not_user_field`: the field is the agent's; leave it out. `confirm.not_required`: the path is wrong or the field has no confirmation of its own; leave it out. Two fields you ask about are like that: a control's `bounds` and `contract.addons.executors`. Do not confirm them; record the user's answer in `discussion.md`. Both are shown in the summary the user signs in stage 5, and the signature covers them.

Never use `dta manifest confirm --all-user` in this stage: it would also confirm the tolerances, stalenesses and ontology types the user has not agreed to yet.

## 4. Check the gate

```sh
dta manifest gaps
```

`result.by_owner.user` lists every user-owned field still `unknown` or `proposed`. Every entry outside the tolerance, staleness and `ontology.type_signature` fields is still open for this stage.

```sh
dta status
```

- Gate `contract` `passed`: go to [stage 2](stage-2-access.md).
- `failed`: each reason reads `<path> is not confirmed` or `contract has no value`. For a field the user already agreed to, confirm it; for any other, ask for it.

If the user later changes a field that was confirmed, write the new value, remove that path's line from `field_status` in `manifest.yaml` (so that it is `proposed` again), show the change to the user and confirm it again: a confirmation is for the value the user saw.
