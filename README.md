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
| `packages/core` | Shared library: structured logging, environment configuration, the manifest schema |
| `packages/toolkit` | The `dta` command line tool; each build stage ends in a gate it evaluates |
| `spikes/storage` | Benchmark that selected Dolt as the versioned store |

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

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker. Python 3.12 is pinned.

```sh
uv sync            # create the environment with every package installed
uv run pytest      # run the tests
uv run ruff check packages && uv run ruff format --check packages
uv run dta --help  # the command line tool
```

Secrets are read only from environment variables. A process that needs a
variable that is missing stops at startup and names it.
