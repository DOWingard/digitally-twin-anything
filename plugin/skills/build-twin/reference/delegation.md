# Delegation

Some work can be handed to other agents working at the same time: profiling a source (stage 3), mapping a source or designing a question's answer (stage 4), and re-modelling a forecast after handover. The tool, not the agents, keeps them apart: each task may change only the manifest keys it owns, writes everything else into its own output directory, and is refused if anything it read changed meanwhile.

Delegate only when you can start other agents and the stage has more than one task; otherwise do the work yourself, as the stage's reference says. Never delegate at stage 6: `dta build` does that stage's work.

## Roles

- You are the orchestrator: you plan, open and merge batches, give each task to one agent, and remain the only one who talks to the user.
- A holder is an agent that claims one task, does it, and submits it. A holder never asks the user anything, never changes a file outside its task's `output/` directory, and never runs a `dta` command that writes, other than `dta tasks claim` and `dta tasks submit`.

## Orchestrate a batch

1. See what can be delegated:

   ```sh
   dta tasks plan --stage <stage>
   ```

   `<stage>` is `3`, `4` or `operate`. `result.batches` lists the batches in the order they must run; each task has `id`, `status`, the manifest paths it reads, `owned_keys` and its `write_zone`. Tasks of one batch can run at the same time.
2. Finish every change you have pending to `manifest.yaml`, the ontology file and `discussion.md`, and run `dta manifest validate` if you changed the manifest. From the moment a batch opens until it is merged or aborted, no file of the workspace outside `tasks/` may change except through `dta`: a hand edit makes every submit and merge refuse (`tasks.foreign.modified`). Keep notes for `discussion.md` aside until then.
3. Open the next batch:

   ```sh
   dta tasks plan --stage <stage> --open
   ```

   `result.opened.tasks` lists the task ids. Optionally write instructions for a task into `<workspace>/tasks/<id>/input/` (plain files) before its holder claims it; from the claim on, that directory must not change.
4. Start one agent per task, with the holder brief below filled in.
5. Follow them:

   ```sh
   dta tasks status
   ```

   lists each task's status and lease, and the proposals stored so far. `dta tasks status --verify` also runs the checks a submit runs.
6. When every task is `submitted`:

   ```sh
   dta tasks merge --all
   ```

   Exit 0: the patches are applied to `manifest.yaml`, one task at a time in id order, and the batch closes. Exit 1: `error.detail` lists every reason per task; see [troubleshooting](troubleshooting.md), "Delegation".
7. Read the proposals the holders stored (`dta tasks status` before the merge, or the tasks' `output/result.json`) and carry out those you agree with yourself. Then run `dta tasks plan --stage <stage>` again: if another batch follows, go back to step 2.
8. A task that cannot be finished: `dta tasks abort <id>`. To give up the whole batch: `dta tasks abort --batch`. Then do that work yourself.

## The holder's brief

Give each holder this text, with the brackets filled in:

> You are `<holder name>`, working on task `<id>` of the digital twin in `<workspace>`, with the `dta` command line tool. Run every `dta` command as `dta --json -w <workspace> <command>`.
>
> 1. Claim the task: `dta tasks claim <id> --holder <holder name>`. Keep `result.token`; never show it to anyone. The paths under `result.paths` are relative to `<workspace>`: `spec` is the task's description (`task.json`), `input` its inputs, `output` the only directory you may write to.
> 2. Read `task.json`: `reads.manifest` lists the manifest paths you may rely on, `reads.ontology_file` the ontology, `writes.owned_keys` the only manifest keys you may set, and `result_format` the shape of your result. Read the inputs.
> 3. Do the work: <the work for this kind of task, from the table below>.
> 4. Write `<output>/result.json` with exactly four keys: `manifest_patch` (an object mapping each dotted manifest path you set to its value; every path inside `writes.owned_keys`), `proposals` (a list of objects with `target` and `description`, for anything you want changed outside your keys), `artifacts` (a list of every other file you wrote under `<output>`, relative to it), and `notes` (text).
> 5. If the work takes longer than your lease (`result.expires_at`), extend it: `dta tasks claim <id> --holder <holder name> --renew --token <token>`.
> 6. Submit: `dta tasks submit <id> --token <token>`. Exit 0: done. Exit 1: `error.detail` lists every reason with its code; fix your output and submit again.
>
> Never ask the user anything, never change a file outside `<output>`, and never edit `manifest.yaml`. When done, report the task id, whether it was submitted, and your notes.

## The work per kind of task

| Task | Stage | The holder | Owned keys |
|---|---|---|---|
| `profile-<source>` | 3 | Runs `dta profile --source <source> --no-write` and sets `sources.<source>.profile` to `result.profiles.<source>`, and `sources.<source>.refresh_period` to that profile's `refresh_period` when it has one. | `sources.<source>.profile`, `sources.<source>.refresh_period`, `sources.<source>.rate_limit_period` |
| `mapping-<source>` | 4 | Writes the mappings of the source as [sources](sources.md), "Mappings", says, from the ontology file and the source's profile, and sets `sources.<source>.mappings`. A class or property the ontology lacks goes into `proposals`. | `sources.<source>.mappings` |
| `question-<question>` | 4 | Designs the question's answer as [functions](functions.md) and [models](models.md) say, and sets `contract.questions.<question>.targets`, `contract.questions.<question>.function`, and for a forecast `contract.questions.<question>.horizon`, `contract.questions.<question>.model`, `models.<question>` and `functions.<question>`. It may also declare functions named `<question>-<suffix>`. The question's `example` is not among its keys: it goes into `proposals`, with the target `contract.questions.<question>.example` and the arguments in the description. | the question's `targets`, `horizon`, `function`, `model`; `models.<question>`; `functions.<question>` and `functions.<question>-*` |
| `remodel-<question>` | operate | Revises the model of a question the twin asked to re-model ([operate](operate.md)), and sets the model's `candidates`, `features` and `backtest`. | `models.<model>.candidates`, `models.<model>.features`, `models.<model>.backtest` |

After merging a stage 4 batch, write each proposed `example` yourself, then write `contract.boundary` and anything else the proposals ask for, and `dta manifest validate`.
