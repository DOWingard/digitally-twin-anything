# Stage 6: build

Goal: the twin running on this machine: its store, its API, its UI, its data loaded, its functions, models and questions registered, and its operator skill generated.

Gate `build` is recorded by `dta build`. It goes stale on any later change to the manifest.

## 1. Build

```sh
dta build
```

It refuses to start unless gates 0 to 5 have passed, then runs these steps in order, each only when what it depends on changed since the last build: `scaffold`, `image`, `store`, `install`, `load`, `api`, `contract`, `sync`, `code`, `functions`, `models`, `questions`, `ui`, `skill`. It starts the twin's containers (named `dta-<id>-…`) and leaves them running.

It can take long: loading a long history and evaluating models take minutes to hours. Let it run; do not start a second build while one runs (the second waits for the workspace lock or refuses with `workspace.locked`).

## 2. Read the outcome

- Exit 0: every step passed; `result.steps` lists each with its `action` (`ran` or `reused`) and `status`. The build gate is recorded. Read the warnings before you go on:
  - in the `models` step, `detail.unaccepted`: a model specification with no accepted candidate, and its question's status;
  - in the `questions` step, `detail.questions`: a question whose `status` is `failing` has a measured error beyond its tolerance.

  These do not stop the build; they make acceptance fail (stage 7). Note them for the user.
- Exit 1, code `build.not_ready`: a gate before the build has not passed. `error.detail.gates` lists them with their reasons; for gates `design` and `gaps`, `error.detail.next` says what to run. Usually gate `design` is `stale` (run `dta manifest validate`) or gate `gaps` says the contract changed since it was signed (stage 5 again).
- Exit 1, code `build.failed`: `error.detail.step` names the failed step; `error.detail.evidence` holds its `message`, and `code` and `detail` when a known error caused it; `error.detail.not_attempted` the steps after it. Look up `error.detail.evidence.code` in [troubleshooting](troubleshooting.md); the table "Build steps" there says what each step's failure usually means.
- Exit 2: the build could not start; look up `error.code`.

## 3. Build again after a fix

After fixing the cause of a failed step, run `dta build` again: the steps that passed are reused, and the build continues from the failed one. To force steps to run again, name the first one:

```sh
dta build --from load
```

`--from models` (or an earlier step) evaluates every model specification again. `dta build --force` reruns every step; use it only when a reused step must be repeated and you do not know which.

If you changed the manifest to fix the cause, validate before building (`dta manifest validate`), and if the change touched `contract` or the ontology's types, the user must agree and sign again (stage 5).

## 4. Look at the running twin when something is unclear

```sh
dta logs api --tail 100
```

shows the last lines each container wrote, with every `.env` value masked; `dta logs` without a service shows all of them. `dta up` starts the stack again after a reboot, `dta down` stops it and keeps its data. Never run `dta down --destroy-data` unless the user asks for it: it deletes the twin's data and resets the build and acceptance gates.

## 5. Check the gate

```sh
dta status
```

Gate `build` `passed`: go to [stage 7](stage-7-acceptance.md).
