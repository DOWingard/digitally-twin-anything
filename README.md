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

Status: under construction. A static twin (loaded once from recorded data) can
be built, accepted and handed over today; live sync, models, fidelity and
control are not built yet.

## Layout

| Path | What it is |
|---|---|
| `packages/core` | Shared library: structured logging, environment configuration, the manifest schema and its checks, ontology loading (LinkML), the versioned store, the connector protocol |
| `packages/toolkit` | The `dta` command line tool: manifest checks, the gate each build stage ends in, delegated tasks, the twin's container stack derived from its manifest, build, acceptance and the operator skill |
| `packages/runtime` | What runs inside a twin's stack: installing and opening a twin, reading its sources, loading them into the graph, the HTTP API and the browser UI |
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

## Building a static twin

A build moves through stages; each ends in a gate, and `dta status` shows where
the build stands. All commands act on a workspace (`--workspace`, default the
current directory) and print one JSON document with `--json`.

| Stage | Gate passes when | Command |
|---|---|---|
| 0 intake | the workspace and a manifest exist | `dta init` |
| 1 contract | the user has confirmed the contract fields | `dta manifest confirm` |
| 2 access | every source can be read from inside the twin's stack | `dta source probe` |
| 3 profiling | every source has a profile in the manifest | `dta profile` |
| 4 design | the manifest and its ontology are consistent | `dta manifest validate` |
| 5 gaps | no required field is open, and the user has signed | `dta manifest sign` |
| 6 build | the stack runs and serves this manifest | `dta build` |
| 7 acceptance | every acceptance check passes | `dta accept` |
| 8 handover | the operator skill is installed | `dta install-skill` |

```sh
dta init
# write manifest.yaml and the ontology file it names (LinkML)
dta ontology check --write     # confirm the ontology's types in the manifest
dta env template               # .env.example and .env; fill in what is not generated
dta source probe               # reach every source from inside the stack
dta profile                    # measure the sources; profiles go into the manifest
dta manifest validate
dta manifest confirm --all-user
dta manifest sign
dta build                      # store, install, load, API, UI, skill
dta accept                     # acceptance report, or a deficit report
dta install-skill              # the skill an agent operates the twin with
```

- Sources are only ever read from inside the twin's stack, never from the host,
  so an address, its credentials and its network mean the same at build time
  and at run time. A source in another compose project is reached through
  `runtime.networks` in the manifest.
- `dta build` can be run again at any time. Every step changes only what
  differs; the load is not repeated while the twin is where the last load left
  it. A failing step stops the build and leaves what was done in place.
- `dta accept` runs every check and reports all failures together: the API
  against its own OpenAPI document (generated requests, fixed seed), the five
  data-readiness requirements, constraint violations in the load, coverage of
  every property the contract names, the UI and the skill.
- `dta install-skill` refuses a twin that has not been accepted unless
  `--provisional` is given; the handover report then lists what has not passed.
- `dta up`, `dta down` and `dta logs` manage the stack. `dta down
  --destroy-data` removes the twin's data and takes back the build and
  acceptance gates.
- No value of `.env` is printed, logged or written to a report.

## What a built twin serves

One HTTP API under `/api/v1`, bound to the loopback address, with one bearer
token. `GET /api/v1/openapi.json` describes every endpoint.

| Group | For |
|---|---|
| `/meta` | identity, health, the manifest and the ontology at a version |
| `/objects` | objects of a type: filter, sort, page; one object with the provenance of each value; its links and its history |
| `/graph` | neighbours, shortest paths, subgraphs, the edge list |
| `/history` | versions, the difference between two versions, branches |
| `/block` | whole node and edge tables, and slices over a range of versions, as Arrow or Parquet |
| `/lifecycle` | the twin's own structured logs |

- Every read takes `at`: `head`, a version id, `#<n>` or a time. A response
  names the version it read, and a cursor keeps reading that version.
- Tables come as JSON (paged), Arrow or Parquet.
- Errors are RFC 9457 problem documents with a code and a trace id.

The browser UI (graph, spreadsheet and history tabs, a version slider, follow
head) reads the twin only through this API; the token stays on its server.

The operator skill is generated from the manifest: how to reach the twin, the
questions it was built to answer with their tolerances and exact definitions,
recipes for every kind of request, and what each error code means.

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
server, and the SQL connector's tests against a real PostgreSQL server. Put
their settings in a `.env` file at the repository root (never committed):

```sh
DTA_STORE_HOST=127.0.0.1
DTA_STORE_PORT=13307
DTA_STORE_USER=root
DTA_STORE_PASSWORD=<choose one>
DTA_STORE_DATABASE=dta_test
DTA_CONNECT_PG_HOST=127.0.0.1
DTA_CONNECT_PG_PORT=15433
DTA_CONNECT_PG_USER=connect_test
DTA_CONNECT_PG_PASSWORD=<choose one>
DTA_CONNECT_PG_DATABASE=connect_test
```

```sh
docker compose -p dta-store-dev --env-file .env -f packages/core/tests/store/compose.yaml up -d
docker compose -p dta-connect-dev --env-file .env -f packages/core/tests/connect/compose.yaml up -d --wait
docker build -f docker/runtime.Dockerfile -t dta-runtime:0.1.0 .   # the image the stack tests run
uv run --env-file .env pytest            # the whole suite
uv run --env-file .env pytest -m scale   # full-size fixtures with performance thresholds
docker compose -p dta-connect-dev --env-file .env -f packages/core/tests/connect/compose.yaml down -v
docker compose -p dta-store-dev --env-file .env -f packages/core/tests/store/compose.yaml down -v
```

- With the `DTA_CONNECT_PG_*` variables set, the PostgreSQL server must be up:
  those tests fail rather than skip when it is not.
- The toolkit's stack tests build small twins in their own compose projects
  (`dta-m3tk-<random>`) on loopback ports 18800 to 18899 and remove them.
  They are skipped when Docker or the image is missing. Rebuild the image
  after changing `packages/core` or `packages/runtime`.
- The UI tests drive headless Chrome through Selenium and are skipped when
  Chrome is not installed.

The testbed is a separate project with its own environment; see
`testbed/README.md`.

Secrets are read only from environment variables. A process that needs a
variable that is missing stops at startup and names it.
