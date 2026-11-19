---
name: build-twin
description: Builds, accepts and hands over a digital twin of a real system with the `dta` command line tool. A twin is a typed graph of the system's objects, links and properties with every past version, kept in step with its data and served by an API, a browser UI and an operator skill. Use when the user points at a database, an HTTP API, a code repository, data files or a described system and asks for a twin, a model, a simulation, a monitor, a forecast or what-if analysis of it. Also use when the user asks to revise, extend, rebuild or repair a twin built before, or when a twin's workspace (a directory holding manifest.yaml and .dta/) is in front of you.
---

# Build a digital twin

## What a twin is

A digital twin is a typed graph of a real system: its objects, the links between them and their properties.
The twin keeps every past version of that graph, so it can say what was true at any time.
It is filled from the system's own data (databases, HTTP APIs, files) and never writes to them.
A static twin is loaded once from recorded data; a live twin reads its sources on a schedule and checks itself against them.
It answers a fixed set of questions, each within an accuracy the user agreed to.

What the user gets at the end:

- an HTTP API on this machine, with a token;
- a browser UI;
- an operator skill installed for their agent, which explains how to use this particular twin;
- a handover report: where the API is, the tier, each question's measured error against its tolerance, what is provisional, and what was not verified.

## How to read this skill

- This file is the map. Each stage has a reference file under `reference/`; open it when you reach that stage and follow it step by step.
- `dta` is on your PATH. Every `dta` command in this skill is written without global options. Run each one as `dta --json -w <workspace> <command>`, for example `dta --json -w <workspace> status`. With `--json`, standard output is one JSON document: `{"ok": true, "result": {...}}` or `{"ok": false, "error": {"code": ..., "message": ..., "detail": {...}}}`. The JSON lines on standard error are the tool's log; read the document on standard output.
- Exit codes: 0 the command did its work; 1 a check or gate ran and failed; 2 the command could not run. On 1 or 2, look up `error.code` in [troubleshooting](reference/troubleshooting.md). 64 or 69 with nothing on standard output: the launcher that starts `dta` refused before the tool ran, and the last line of standard error, which starts with `dta:`, says why; look it up under "The launcher" in troubleshooting.
- Text in angle brackets is a value you substitute: `<workspace>` is the twin's workspace directory, `<question>` a question id, `<source>` a source id. Never type the brackets.
- A field path such as `contract.questions.<question>.tolerance.value` names one field of `manifest.yaml`: each dot is one level of YAML nesting, and `<question>` is a key you chose.
- You write `manifest.yaml`, the ontology file, the twin's code directory and `discussion.md` with your file tools. Everything else in the workspace is written by `dta`. Commands that write to `manifest.yaml` rewrite the whole file, so read it again before you edit it after one.
- [manifest.md](reference/manifest.md) lists every manifest field, who owns it and when it is required. It is generated from the tool and always current.

## The first command on a machine

`dta` runs from two container images that are built on the user's machine, not downloaded ready-made. The first `dta` command there, usually the `dta status` of "Start or resume", builds them before it does its own work:

- It takes several minutes. It needs Docker running and network access to Docker Hub (`registry-1.docker.io`, `auth.docker.io`, `production.cloudfront.docker.com`) and to the Python package index (`pypi.org`, `files.pythonhosted.org`). It downloads about 0.6 GB, and the images take about 1.8 GB of disk.
- Before you run it, tell the user in one or two sentences: the first command builds the twin tool's images on this machine, which takes several minutes and about 0.6 GB of downloads; later commands start within seconds.
- Give that call a timeout of 10 minutes (600000 ms), or the longest your shell tool allows. Docker's progress goes to standard error; standard output is still the one JSON document.
- If the call is cut off, run the same command again: docker keeps the build steps that finished. Only one build runs at a time; a second `dta` command started meanwhile waits for it and then uses the images.
- A failed build exits 69 with nothing on standard output; the last line of standard error starts with `dta: building` and names the log file. Do what "The launcher" in [troubleshooting](reference/troubleshooting.md) says.

## Ground rules

Each rule has its reason; follow the rule even when the reason seems not to apply.

1. Start every session, and resume after every interruption, with `dta status`: the workspace, not your memory, says which stage the build is at.
2. Work only on the stage `dta status` reports, and move on only when its gate shows `passed`: a later stage built on a failed gate is built on facts nobody checked.
3. Never edit, delete or create anything under `.dta/` or `tasks/` by hand: they hold the recorded gates, signatures and leases, and a hand edit forges them.
4. The user owns the contract (everything under `contract`) and the ontology's types; you propose, the user decides, and only the user's agreement lets you run `dta manifest confirm`: the twin is judged against what the user agreed, not against what you guessed.
5. Ask the user only for what fills a manifest field, one question at a time, each with a proposed default, and never ask what you can find out yourself: every question costs the user time, and a default makes the answer cheap.
6. Speak to the user in one voice: only you, never a delegated agent, asks the user anything or reports to them, so the user sees one consistent conversation.
7. Write every question you ask the user, and the answer, to `<workspace>/discussion.md` with the field it fills (format below): the record shows later why each value is what it is.
8. Secrets only by name: you name each variable and what it is for, the user writes its value into `<workspace>/.env` themselves; never read `.env`, never ask for a secret value in the conversation, never print, log or repeat one: a secret that reaches the conversation is no longer secret.
9. The target system is read-only for you until the twin is built: reach it only through `dta source probe` and `dta profile`, never connect to a database or API yourself and never change a source file: the twin must not alter what it observes, and the credentials are not yours to use.
10. When a question's measured error exceeds its tolerance, tell the user the measured error, the tolerance and the options (see [stage 7](reference/stage-7-acceptance.md)); never widen a tolerance yourself; say "provisional" for every answer that is: the user decides with the true numbers.
11. Delegate work to other agents only as `dta tasks plan` lists it and only through `dta tasks` commands ([delegation](reference/delegation.md)): the tool keeps delegated work from overwriting shared files.
12. Never sign for the user without their explicit yes: sign only after showing them the summary from `dta manifest summary`, and only with their name ([stage 5](reference/stage-5-gaps.md)).

## The record of the discussion

Create `<workspace>/discussion.md` right after `dta init`. Append one entry per question you ask, in this form, and never rewrite an earlier entry:

```markdown
## <UTC date and time>, stage <n>
Field: `contract.questions.<question>.tolerance.value`
Asked: <the question as you put it to the user>
Proposed: <the default you proposed>
Answer: <the user's answer, in their words>
Written: <the value you wrote, or "nothing" and why>
```

When one answer fills several fields, list every field path on the `Field:` line, separated by commas. For what the user said without being asked, write `Asked: not asked; the user said it` and `Proposed: nothing`. While a delegation batch is open, do not write to `discussion.md` (the tool refuses foreign changes then); keep the entries and append them after the batch is merged or aborted.

## The stages

Every stage ends in a gate. `dta status` lists them in `result.gates`, each with its `stage`, `name`, `reasons` and `result`: `passed`, `failed`, `stale` (passed for an earlier manifest; run that stage's recording command again) or `not_evaluated`. The current stage is the lowest one whose gate has not passed.

| Stage | Purpose | Reference | Commands | Gate |
|---|---|---|---|---|
| 0 | Name the system, what the twin is for and where its data is | [stage-0-intake.md](reference/stage-0-intake.md) | `dta status`, `dta init` | intake |
| 1 | Agree what the twin must answer, how accurately and how fresh | [stage-1-contract.md](reference/stage-1-contract.md) | `dta manifest gaps`, `dta manifest confirm` | contract |
| 2 | Reach every source read-only from the twin's stack | [stage-2-access.md](reference/stage-2-access.md) | `dta env template`, `dta env check`, `dta source probe` | access |
| 3 | Measure what each source holds | [stage-3-profiling.md](reference/stage-3-profiling.md) | `dta profile` | profiling |
| 4 | Design the ontology, mappings, functions, models and live settings | [stage-4-design.md](reference/stage-4-design.md) | `dta ontology check`, `dta sync derive`, `dta manifest validate` | design |
| 5 | Close every gap and have the user sign the contract | [stage-5-gaps.md](reference/stage-5-gaps.md) | `dta manifest validate --complete`, `dta manifest summary`, `dta manifest sign` | gaps |
| 6 | Build and start the twin | [stage-6-build.md](reference/stage-6-build.md) | `dta build` | build |
| 7 | Check the running twin against the contract | [stage-7-acceptance.md](reference/stage-7-acceptance.md) | `dta accept` | acceptance |
| 8 | Install the operator skill and report to the user | [stage-8-handover.md](reference/stage-8-handover.md) | `dta install-skill`, `dta questions list` | handover |

The design stage draws on these references: [ontology](reference/ontology.md), [sources and mappings](reference/sources.md), [functions](reference/functions.md), [models](reference/models.md), [live twins](reference/live.md). The questions to ask the user are in the [question bank](reference/question-bank.md). Two complete workspaces to compare against are in [examples](reference/examples.md).

Recorded gates go stale when the manifest changes. Gate 4 (design) is recorded by `dta manifest validate` and goes stale after any change to `manifest.yaml`, including `dta manifest confirm`: after every change, run `dta manifest validate` again before you read `dta status`.

## Start or resume

1. Run `dta status`. On a machine where `dta` has never run, this call builds the images first: see "The first command on a machine" above.
   - Exit 2 with code `workspace.missing`: there is no workspace yet. Go to [stage 0](reference/stage-0-intake.md).
   - Exit 0: read `result.stage.number`. If it is a number, open that stage's reference file and continue there. If `result.stage.name` is `complete`, every gate passed: the twin is handed over; go to "After handover" below.
2. If the user named a workspace directory, use it; if they did not, and a directory under the current one holds `manifest.yaml` and `.dta/`, use that one and say so.
3. Read `<workspace>/discussion.md` before you ask anything: never ask again what is already answered there.

## When a command fails

1. Read `error.code` and `error.detail` of the JSON document (with exit 1, `error.detail` also holds the command's full result).
2. Find the code in [troubleshooting](reference/troubleshooting.md) and do exactly what its row says. The row also says whether the user must be asked.
3. A code that is not in troubleshooting: run the command once more. If it fails the same way, tell the user the command, the code, the message and `trace_id`, and stop the build until the user says how to go on.
4. Never work around a refusal by editing files the tool writes (`.dta/`, `tasks/`, `stack/`, `compose.yaml`, `.env.example`, `skill/`): the next command would overwrite or reject the edit.

## Handover

At stage 8 ([stage-8-handover.md](reference/stage-8-handover.md)) you run `dta install-skill`, then report to the user, in this order:

1. Where the twin is: the API address (`http://127.0.0.1:` and the port the user set in `DTA_API_PORT`), the variable holding the token, and the UI port if the UI is served.
2. The tier: static (loaded once) or live (and how often it syncs, from `sync.interval`).
3. Each question: its id, its text, its measured error against its tolerance (or that it was not measured), and its status and basis, from `dta questions list`.
4. What is provisional: every question whose status is not `validated`, every model not accepted, every gate not passed, with the reason.
5. What was not verified: anything the acceptance report lists as skipped or not measured, and anything you assumed without the user's word.
6. Where the operator skill is installed and that it is read by the user's agent from now on.

## After handover

The build agent is done when the report is given. Later requests come back to this skill:

- A change to what the twin must do (a new question, a tolerance, a control, the purpose) or to the ontology's types is a contract revision: follow "Revise the contract" in [stage-8-handover.md](reference/stage-8-handover.md).
- A change to how it does it (a mapping, a function, a model, a sync setting) is a design revision: follow "Revise the design" in the same file.
- Running and watching the twin (fidelity incidents, a model that drifted, actions waiting for approval): [operate.md](reference/operate.md).
