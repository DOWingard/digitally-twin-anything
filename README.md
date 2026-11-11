# digitally-twin-anything

An agent plugin that builds a working digital twin of anything that can be
described by a large enough dataset, then keeps it accurate and manages it.

A twin built by this system is:

- a typed graph of the target (objects, links, properties) with its full
  version history, so every read can ask "as of" any past version;
- synced from the target's data sources on a schedule (a live twin) or loaded
  once (a static twin);
- able to answer a declared set of questions within declared error tolerances,
  and to correct itself or raise a failure when it cannot;
- served through one HTTP API, a browser UI and a generated skill file that
  tells an agent how to operate it;
- one container stack that can be exported and moved to another host.

Status: under construction. The parts present so far are listed below.

## Layout

| Path | What it is |
|---|---|
| `packages/core` | Shared library: structured logging, environment configuration, the manifest schema and its checks, ontology loading (LinkML), the versioned store, the connector protocol |
| `packages/toolkit` | The `dta` command line tool: manifest checks, the gate each build stage ends in, delegated tasks, and the twin's container stack derived from its manifest |
| `packages/runtime` | What runs inside a twin's stack: installing and opening a twin, and (as they are added) sync, the HTTP API and the browser UI |
| `docker/runtime.Dockerfile` | Image of the twin's services: one image, one command per service |
| `testbed` | A simulated water utility used as the standing test target, with a reference twin definition in `testbed/twin` |
| `spikes/storage` | Benchmark that selected Dolt as the versioned store |

## The store

A twin's graph lives in a [Dolt](https://github.com/dolthub/dolt) database, one
table per object or link type, generated from the twin's ontology.

- A version is one commit. Its time is data time (the latest data timestamp the
  twin had seen), never the wall clock, so history can be replayed exactly.
- Every stored value carries its provenance: where it came from, whether it
  was observed, inferred, assumed or simulated, how confident, and when it
  was true.
- A change set is validated against the ontology and written whole or not at
  all. A committed version never changes.
- Reads take a selector (`head`, a version id, `#<n>`, a time, or a range)
  and return Arrow tables.
- Branches hold scenarios; simulated facts cannot reach the live branch.

## The manifest

A twin is fully specified by one file, `manifest.yaml`, in its workspace. Each
required field has a status:

| Status | Meaning |
|---|---|
| unknown | no value yet |
| proposed | a value is set and waits for the user to confirm it |
| derived | computed by the toolkit from measurements |
| confirmed | accepted by the field's owner (the user or the building agent) |

The build may start only when no required field is unknown or proposed.

`dta manifest validate` reports what is inconsistent in the manifest as
written; `dta manifest gaps` lists every required field still unknown or
waiting for confirmation; `dta status` shows the stage the build stands at
and the result of each stage's gate.

## Delegated tasks

Parts of a build (profiling a source, designing a mapping, training a model
candidate) can be handed to other agents working at the same time. The
toolkit, not the agents, keeps them from interfering:

- a task declares the manifest paths it reads and the manifest keys it owns;
  its only other write zone is `tasks/<id>/output/`;
- tasks share a batch only when none writes what another reads or writes;
- one holder per task, by a lease that expires unless renewed;
- a task never changes shared state: it returns a patch for its own keys and
  proposals for anything else, and the patches are applied one at a time;
- a result is refused, with every reason listed, if it touches anything
  outside its zone or if what it read has changed since the batch opened.

```sh
dta tasks plan --open              # open the next batch of the current stage
dta tasks claim <id> --holder <name>
dta tasks submit <id> --token <token>
dta tasks merge --all
dta tasks status --verify
```

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker. Python 3.12 is pinned.

```sh
uv sync            # create the environment with every package installed
uv run pytest      # run the tests; those that need a database are skipped
uv run ruff check packages && uv run ruff format --check packages
uv run dta --help  # the command line tool
```

Tests of the store and of everything built on it run against a real Dolt
server. Put its settings in a `.env` file at the repository root (never
committed):

```sh
DTA_STORE_HOST=127.0.0.1
DTA_STORE_PORT=13307
DTA_STORE_USER=root
DTA_STORE_PASSWORD=<choose one>
DTA_STORE_DATABASE=dta_test
```

```sh
docker compose -p dta-store-dev --env-file .env -f packages/core/tests/store/compose.yaml up -d
uv run --env-file .env pytest            # the whole suite
uv run --env-file .env pytest -m scale   # full-size fixtures with performance thresholds
docker compose -p dta-store-dev --env-file .env -f packages/core/tests/store/compose.yaml down -v
```

The testbed is a separate project with its own environment; see
`testbed/README.md`.

Secrets are read only from environment variables. A process that needs a
variable that is missing stops at startup and names it.
