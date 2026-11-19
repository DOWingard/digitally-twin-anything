# Operate a twin after handover

Once handed over, the twin is used through its operator skill, which the user's agent reads. This file is for you, the agent that built it, when the user comes back because something needs the builder: the twin reports trouble, a model drifted, an action waits for a decision, or the stack must be started again.

## Look first

```sh
dta status
dta fidelity status
dta questions list
```

`dta status`: every gate should be `passed`. A `stale` gate means the manifest was changed after the build; finish that revision ([stage 8](stage-8-handover.md)). `dta fidelity status` gives the twin's state: `healthy`, `correcting`, `failed` or `unknown` (no check has run yet), and what the last check found. `dta questions list` gives each question's status and measured error.

For a live twin also:

```sh
dta sync status
dta notifications
```

`dta sync status` shows the twin's clock, how fresh its data is and each source's last run; `dta notifications` the failures it raised and whether they were delivered.

## The stack

- After a reboot, or when `dta status` works but the API does not answer: `dta up`.
- To stop the twin and keep its data: `dta down`.
- To see what a container wrote: `dta logs <service> --tail 200`, where `<service>` is `api`, `store`, `ui`, `scheduler` or `worker`.
- `dta down --destroy-data` deletes the twin's data; run it only when the user asks for exactly that, after saying the twin must then be built and accepted again.

## Incidents

```sh
dta fidelity incidents --status unresolved
dta fidelity incident <incident>
```

An incident has a `kind` (`breach`: a question outside its tolerance; `stale`: data older than a staleness allows; `signal`: a sensor stuck or drifting; `reconcile`: an executed action whose effect was not observed) and a status (`open`, `remodel_requested`, `failed`, `resolved`). `dta fidelity incident <incident>` lists every step the twin took.

| Status and kind | What to do | Ask the user? |
|---|---|---|
| `open`, any kind | The twin is correcting by itself; report it to the user and look again after the next check. | No. |
| `remodel_requested` | The twin re-fitted and re-selected and still misses the tolerance; it waits for you. Find the cause and revise the model, below. | Yes: tell them what you found and what you change. |
| `stale` | A source stopped delivering. Run `dta sync status` and `dta sync run`; tell the user which source and since when. | Yes: the source is theirs. |
| `signal` | A sensor looks stuck or drifting: `dta fidelity signals`. Tell the user which one. When the user says it is mended: `dta fidelity clear <signal> --by "<user's name>"`. | Yes. |
| `reconcile` | An action was sent and its effect not observed. `dta actions records --status executed`; tell the user. When the user says it is mended: `dta fidelity resolve <incident> --by "<user's name>"`. | Yes. |
| `failed` | The incident ran out of corrections, or nobody re-modelled in time: the question's answers are unreliable. Tell the user at once, with the incident's steps. A `breach` incident resolves by itself once a model fitted after the failure meets the tolerance (revise the model, below). Once the cause is mended and the user agrees to start the corrections again: `dta fidelity retry <incident> --by "<user's name>"` for kind `breach` or `stale`; `dta fidelity resolve <incident> --by "<user's name>"` for kind `signal` or `reconcile`. | Yes. |

Never resolve, clear or retry anything without the user's word, and never raise a tolerance to make an incident go away.

## Revise a model when the twin asks

1. Read the incident and the question's errors: `dta fidelity incident <incident>`, `dta fidelity errors <question>`, `dta models list`. Did the error rise at once (something changed in the system) or slowly (a drift)?
2. Ask the user what changed in the real system around that time. Name a cause before changing anything.
3. Revise only the model: `models.<model>.candidates`, `models.<model>.features`, `models.<model>.backtest` ([models](models.md)). With other agents, `dta tasks plan --stage operate` offers a `remodel-<question>` task for it ([delegation](delegation.md)).
4. `dta manifest validate`, then `dta build`. Read the `models` step: the revised specification must have an accepted candidate.
5. The twin judges the new model by itself over its next checks; follow with `dta fidelity incident <incident>`. Tell the user what you changed and why.

If the cause lies in the contract (the system now needs a different tolerance or question), that is the user's revision of the contract ([stage 8](stage-8-handover.md)).

## Actions and control loops

- Proposed actions waiting for a person: `dta actions records --status proposed`, one in detail with `dta actions record <uid>`. Show them to the user; approve or reject only on the user's word, with their name: `dta actions approve <uid> --by "<user's name>"` or `dta actions reject <uid> --by "<user's name>" --note "<why>"`.
- Loops: `dta control list`. A loop is registered disabled; `dta control enable <loop> --by "<user's name>"` only when the user says to start it, `dta control disable <loop> --by "<user's name>"` when they say to stop it.
- To propose an action on the user's request: `dta actions propose <action> --object <uid> --param value=<value> --reason "<why>" --source "<user's name>"`.


## Move the twin to another machine

A twin is moved as one file. The file holds the workspace (manifest, ontology, code, the discussion, the reports), every version of the twin's data and its fitted models. It holds no secret: `.env` stays behind.

On the machine that has the twin, with the twin running:

```sh
dta export --out <folder>
```

- Exit 0: `result.bundle` is the file written, `result.head` the newest version it holds. Tell the user every entry of `result.warnings`: a source that is a folder on this machine is not in the file, and the user must bring that folder along themselves.
- The twin keeps running during the export.

On the other machine, where the plugin and the twin's runtime image are installed:

```sh
dta import <bundle> --into <workspace>
```

1. The first run unpacks the workspace and creates `.env` with a new store password and API token; `result.status` is `unpacked`. `result.to_fill` names what the user must write, as in [stage 2](stage-2-access.md) step 4: the ports, the base address and every source credential. Have the user fill them in.
2. Run the same command again. It starts the store alone, restores every version, and checks that the newest version of every branch is the one exported. Exit 0 with `result.status` equal to `verified` means the twin's data arrived unchanged; `result.next` lists the commands that follow.
3. The gates `access`, `build`, `acceptance` and `handover` are not carried: on the new machine run `dta source probe`, `dta build`, `dta accept` and `dta install-skill`, as stages 2, 6, 7 and 8 say. The user's signature is carried and stays valid while the contract is unchanged.

Never pass `--replace-data` unless the user said, for this twin on this machine, that its existing store may be deleted.
