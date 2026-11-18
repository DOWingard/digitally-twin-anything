# Stage 7: acceptance

Goal: evidence that the running twin does what the contract says: its API answers as described, its data is all there and connected, every question is answered within its latency budget and is validated or honestly provisional, and, for a live twin, it syncs, checks itself and can raise failures.

Gate `acceptance` is recorded by `dta accept`. It goes stale on any later change to the manifest.

## 1. Run every check

```sh
dta accept
```

It runs every check against the running twin and writes a report into the workspace.

- Exit 0: every check passed. `result.report` is `reports/acceptance.json`, with `reports/acceptance.md` beside it. Go to [stage 8](stage-8-handover.md).
- Exit 1, code `accept.failed`: `error.detail.failed` lists the ids of the failed checks; `error.detail.checks` holds every check with `id`, `passed`, `examined` (what it looked at), `evidence` and, when failed, `remedy` (what to change). The same is in `reports/deficit.md`.
- Exit 1, code `accept.not_built`: the twin is not built for the current manifest; run `dta build` (stage 6).

## 2. Route each failed check

Read each failed check's `remedy` and `evidence`, then act by its id:

| Check | What failed | What to do | Ask the user? |
|---|---|---|---|
| `api.contract` | The API answered outside its own description. | Run `dta logs api --tail 200`; rebuild with `dta build --from api`. If it fails again, report the evidence to the user and stop. | Only if it persists. |
| `readiness.R1` | The served schema differs from the ontology file. | `dta build --from install`. | No. |
| `readiness.R2` | A mapped class has no objects, or an object cannot be read. | Check that class's mappings (identity columns, required properties) against the profile; `dta build --from load`. | No. |
| `readiness.R3` | No links: no object with neighbours one and two steps away. | Map the reference properties (or a link class) that connect the objects ([ontology](ontology.md)); `dta build --from load`. | No, unless the data holds no relation at all. |
| `readiness.R4` | No history: no object has two points in time in a timed property. | Set `sync.backfill_interval` ([live twins](live.md)) and check the `valid_time` columns; `dta build --from load`. | No. |
| `readiness.R5` | The whole-table exports failed. | `dta logs api --tail 200`; report to the user if it persists. | Only if it persists. |
| `data.constraints` | Stored rows break the ontology's bounds or checks. | Read the evidence for the class and property; fix the mapping (unit, column) or, if the source holds such values, the ontology's bounds; `dta build --from load`. | Yes, if a bound must change: it says what the system can be. |
| `data.coverage` | A boundary observation or question target has no value at the newest version. | Map that property, or fix the mapping's column; `dta build --from load`. | No. |
| `ui.reachable` | The browser UI did not answer. | `dta logs ui --tail 100`; check `DTA_UI_PORT` is free (ask the user to change it in `.env`); `dta build --from ui`. | Only to change the port. |
| `skill.current`, `skill.endpoints` | The generated operator skill is missing, from an older manifest, or names endpoints the API lacks. | `dta build --from skill`. | No. |
| `sync.scheduler`, `sync.runs`, `sync.sources`, `sync.freshness` | A live twin does not sync on schedule, a source failed, or data is older than a staleness allows. | Read the evidence; `dta sync status`; `dta sync run` to try one run and read its error code. A source that delivers late: tell the user. | Yes, if a source delivers later than a staleness allows. |
| `questions.answered` | A question could not be answered with its `example`, or not within its latency budget. | Fix the example's arguments or the function's parameters ([functions](functions.md)); raise `latency_budget` only if the function is as cheap as it can be; `dta build --from functions`. | No. |
| `questions.error` | A question is `failing`: its measured error is beyond its tolerance. | Tell the user (below). | Yes. |
| `models.accepted` | A model specification has no accepted candidate. | Tell the user (below), with the best backtest error from `dta models list`. | Yes. |
| `compute.worker` | Registered code could not run in the worker. | `dta logs worker --tail 200`; fix the code or `runtime.worker_requirements`; `dta build --from code`. | No. |
| `fidelity.state`, `fidelity.schedule` | A live twin's self-check is not healthy or not scheduled. | `dta fidelity status`; [operate](operate.md). | If an incident needs the user. |
| `actions.registered` | An action, control or loop of the manifest is not registered as declared. | `dta register`, then `dta accept` again. | No. |
| `notifications.configured` | Failures cannot be raised where the contract says. | Check that the webhook variable is set (`dta env check`); `dta notifications` shows the last attempts. | Yes, if the webhook address is wrong. |
| `sdk.usable` | The generated Python client did not install into a fresh environment, or could not read an object or answer a question; `evidence.output` ends with its error. | `dta generate sdk --force`, then `dta accept`; if it fails again, show the evidence to the user. | Only if it persists. |
| `mcp.reachable` | The MCP adapter did not answer, offered other tools than the API's operations, or did not pass the caller's token on. | `dta logs mcp --tail 100`; `dta build --from mcp`. | If a port is in use. |
| `sql.readonly` | The reader could not sign in, could not read, or was allowed a write. | `dta build --from store`, then `dta accept`; if the reader was allowed a write, stop and show the evidence to the user. | Yes, if a write was allowed. |

After each fix: if you changed the manifest, `dta manifest validate` (and stage 5 again if the change touched `contract`), then the `dta build --from <step>` the table names, then `dta accept` again.

## 3. A question outside its tolerance

Never change a tolerance yourself, and never call a failing question passed. Tell the user, for each failing question or model without an accepted candidate:

> Question `<question>` ("<text>") is measured with an error of <measured_error> against the agreed tolerance of <tolerance> (<metric>). The options are:
> 1. Accept a larger tolerance of <value> (a change to the contract, which you sign again).
> 2. Let me try to improve it: <what you would change: more candidates, other features, a shorter backtest step, another mapping>. This takes another build.
> 3. Keep the question, marked provisional: the twin answers it and says every answer is provisional.
> 4. Drop the question.
>
> Which do you choose?

Record the answer in `discussion.md`. Then: option 1 is a contract revision ([stage 8](stage-8-handover.md), "Revise the contract"); option 2 is a design revision ([stage 8](stage-8-handover.md), "Revise the design", which also says how it is done before handover); option 3 sets `contract.questions.<question>.provisional: true` and rebuilds from `models`; option 4 removes the question, its model and its function, and is a contract revision.

## 4. What `validated` rests on

`questions.error` passes for a question that is `validated` whatever its `basis`. A question answered by a function alone, whose data has no noise figure, is validated on `basis: latency` with `measured_error` null: it answered within its latency budget, and its error against the tolerance was never measured. That is not a failure and needs no fix, but the handover report must say it ([stage 8](stage-8-handover.md), "Gather the numbers").

## 5. Check the gate

```sh
dta status
```

Gate `acceptance` `passed`: go to [stage 8](stage-8-handover.md).
