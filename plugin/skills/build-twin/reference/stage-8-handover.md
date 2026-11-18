# Stage 8: handover, and revisions after it

Goal: the operator skill installed where the user's agent reads it, and a report that tells the user what they have, how good it is, and what is not yet proven.

Gate `handover` is recorded by `dta install-skill`. It passes only when gates 0 to 7 have passed.

## 1. Install the operator skill

The build generated the operator skill into `<workspace>/skill/twin-<id>/` (the twin's id with every character other than a lowercase letter or digit turned into `-`), and the user's agent will know it by that name. Install it:

```sh
dta install-skill
```

It installs into the user's personal skills directory, read by their agent in every project. If the user said the twin belongs to one project only, use `dta install-skill --scope project` instead, run with the project's directory as the current directory.

- Exit 0: `result.installed` is where it went; `result.handover` is `reports/handover.md` in the workspace.
- Exit 1, code `handover.not_accepted`: a gate before handover has not passed; `error.detail.gates` lists them. Go back to that stage. Use `--provisional` only when the user asks for the skill before acceptance passes, and say so in the report: the handover gate then stays failed.
- Exit 1, code `skill.stale` or `skill.missing`: the manifest changed after the build; run `dta build --from skill`, then `dta accept`, then install again.

## 2. Gather the numbers

```sh
dta questions list
```

`result.questions` holds, per question, `id`, `status` (`validated`, `provisional` or `failing`), `measured_error`, `tolerance`, `metric`, `unit` and `reason`. Read also `reports/acceptance.md` for anything the checks marked as a warning, and `reports/handover.md`, which the tool wrote.

## 3. Report to the user

Write the report in this order, plainly, and give the same text to the user:

```markdown
# <name>: your twin is ready

- API: http://127.0.0.1:<API port> (the port you set as DTA_API_PORT); token in DTA_API_TOKEN in <workspace>/.env
- Browser UI: http://127.0.0.1:<UI port> (DTA_UI_PORT)
- Tier: <static: loaded once from data up to <history end> | live: syncs every <sync.interval>>
- Operator skill: <result.installed>; your agent reads it from now on.

## Questions
| Question | Measured error | Tolerance | Status |
|---|---|---|---|
| <id>: <text> | <measured_error> | <tolerance> (<metric>) | <status>: <reason> |

## Provisional
<every question not validated, every model not accepted, every gate not passed, with the reason; or "Nothing is provisional.">

## Not verified
<what acceptance did not measure, and every assumption you made without the user's word, from discussion.md; or "Nothing.">
```

Never round a measured error towards the tolerance and never leave out a provisional item. Then run `dta status`: every gate is `passed` and `result.stage.name` is `complete`.

## Revise the contract

A revision of the contract changes anything under `contract`, or the ontology's types. The user owns it.

1. Ask what is to change ([question bank](question-bank.md)), write it, record it in `discussion.md`, and for each changed field that was confirmed, remove its line from `field_status` so it is proposed again.
2. Increase `twin.revision` by 1.
3. Carry the change through the design (stage 4): new questions need targets, functions, examples and, for forecasts, models; new types need mappings. Then `dta manifest validate`.
4. Stage 5 again: show the changed fields, confirm what the user agrees to, `dta manifest validate --complete`, show the new `dta manifest summary`, and sign only on the user's yes.
5. `dta build`, `dta accept`, `dta install-skill`, and report again (steps 2 and 3 above), saying what changed.

## Revise the design

A revision of the design changes mappings, functions, models, sync or fidelity settings, actions, loops or code, and nothing under `contract`. It needs no new signature.

1. Make the change; record why in `discussion.md` if the user asked for it.
2. Increase `twin.revision` by 1.
3. `dta manifest validate` (exit 0).
4. `dta build` (it redoes only the steps the change affects), `dta accept`, `dta install-skill`, and report the change and any changed numbers to the user.
