# Stage 2: access

Goal: every source can be read, read-only, from inside the twin's own containers, with secrets that only the user has seen.

Gate `access` is recorded by `dta source probe` run without `--source`. It goes stale when a source's `connector`, `location`, `env` or `options`, or `runtime.networks`, changes: probe again after any such change.

## 1. Check that containers can run

The twin, and every command from here on that touches a source, runs in containers on this machine.

```sh
docker version --format '{{.Server.Version}}'
```

- Exit 0 and a version number: Docker runs. Go on.
- Anything else: tell the user "The twin runs in Docker containers, and Docker is not running or not installed on this machine. Please start it, then tell me." Wait for the user, then run it again.

```sh
docker image inspect --format '{{.Id}}' dta-runtime:0.1.0
```

Use the value of `runtime.image` in `manifest.yaml` in place of `dta-runtime:0.1.0` if it differs.

- Exit 0: the runtime image is on this machine. Go on.
- Exit 1 ("No such image"): tell the user "The twin's runtime image `<image>` is not on this machine. It is installed with the `dta` toolkit; please install or build it, then tell me." Wait, then run it again. Without it no source can be reached.

## 2. Complete the source options needed to read

A file source must declare its datasets before it can be read, and so must an HTTP source; a SQL source can be probed without, and then lists every table. Write `sources.<source>.options` for each file and HTTP source now, as [sources](sources.md), "Options", says. If you do not know a file name or an endpoint path, ask the user, or for a repository, read it from the repository.

## 3. Create the environment file

```sh
dta env template
```

Exit 0. It writes `<workspace>/.env.example` (every variable with its description, no values) and creates or extends `<workspace>/.env` without changing any line already there. `result.generated` names the values it generated itself (the store's password and the API token); `result.to_fill` names those the user must write.

## 4. Have the user fill in `.env`

Never open, read or write `<workspace>/.env` yourself. Tell the user, in one message, what to write after the `=` of each name in `result.to_fill`:

- For a variable with `secret: false` in `env`, give the exact value to write: a folder's absolute path, a host name, a port number.
  - `DTA_API_PORT`: a free port on this machine for the twin's API; propose `8780`.
  - `DTA_UI_PORT`: a free port for the browser UI; propose `8781`.
  - `DTA_BASE_URL`: `http://127.0.0.1:` followed by the API port, such as `http://127.0.0.1:8780`.
- For a variable with `secret: true`, say only what it is (from `env.<NAME>.description`) and where the user can find it; never ask for the value and never suggest one.

Use this message, with the list filled in:

> Please open `<workspace>/.env` in your editor and write these values after the `=` of each line. The file stays on this machine and I never read it.
> - `DTA_API_PORT=8780` (a free port for the twin's API)
> - `<NAME>=` <what it is, and where to find it>
> Tell me when you are done.

When the user says they are done:

```sh
dta env check
```

- Exit 0: every required variable is set. If `result.warnings` holds an entry with code `env.permissions`, other users of the machine can read the file: run `chmod 600 <workspace>/.env` (it changes who may read the file, not what is in it) and run the check again.
- Exit 1, code `env.missing`: `error.detail.missing` lists the names still empty. Tell the user which ones, and run the check again when they say done.

If the user pastes a secret into the conversation, do not repeat it; tell them it should go into `.env` instead and that they may want to change it, since it has now been shared.

## 5. Probe the sources

```sh
dta source probe
```

It reads a few rows of every dataset, from inside the twin's stack, and records the gate.

- Exit 0: every source answered; `result.sources` lists each with its datasets. Gate `access` is passed. Go to [stage 3](stage-3-profiling.md).
- Exit 1, code `access.failed`: `error.detail.failing` lists each source (and dataset) that could not be read, with its own `error_code` and `error`. Look up each `error_code` in [troubleshooting](troubleshooting.md); the usual ones are `source.unavailable` (address not reachable from a container), `source.auth_failed` (credentials refused), `source.invalid_config` (options or location wrong) and `config.missing_env` (a variable is not in `.env`). Fix, then probe again.
- Exit 2 with a code starting `stack.`: Docker could not run the probe; see [troubleshooting](troubleshooting.md).

To retry one source while fixing it, use `dta source probe --source <source>`; it records no gate, so finish with `dta source probe` for all sources.
