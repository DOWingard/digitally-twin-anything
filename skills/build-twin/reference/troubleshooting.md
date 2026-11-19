# Troubleshooting

Look up the `error.code` of a failed command here. For `validate.failed`, `accept.failed`, `build.failed`, `tasks.submit.refused` and `tasks.merge.refused`, also look up each code inside `error.detail`. When a code is not listed: read `error.message`, which names the field or file concerned; if it still does not say what to change, show the message to the user and stop.

The last column says whether the user must be asked. "No" means fix it yourself and run the command again.

## The launcher

`dta` is started by a launcher that runs the tool's container image, and builds the images first when they are not on the machine ("The first command on a machine" in the [skill](../SKILL.md)). When a command exits 64 or 69 and prints nothing on standard output, the launcher refused before the tool ran: look up the last line of standard error, which starts with `dta:`, by what it says.

| The last line says | Meaning | What to do | Ask the user? |
|---|---|---|---|
| `this machine is <system> and the tool runs on Linux only` | The plugin is tested on Linux only; on other systems Docker runs containers in a virtual machine, where the tool cannot reach a twin the way it does on Linux. | Tell the user it needs a Linux machine (a Linux server or virtual machine with Docker works). Do not set `DTA_UNSUPPORTED_OS` unless the user asks to try it untested. | Yes. |
| `docker is not installed or not on the PATH` | Docker is not on this machine, or not where the shell finds it. | Ask the user to install Docker: the engine, its command line and the buildx plugin (Docker Desktop has all three). | Yes. |
| `the docker daemon is not reachable` | Docker is installed but its daemon is not running, or not at the socket the line names. | Ask the user to start Docker, then run the command again. | Yes. |
| `docker refuses this user` | The user's account may not use Docker's socket. | Tell the user the line: they add their account to the socket's group (often `sudo usermod -aG docker "$USER"`, then log in again) or use rootless Docker. Never run `dta` with `sudo`. | Yes. |
| `docker cannot name the daemon it talks to` | Docker's settings name no usable daemon. | Ask the user to check `DOCKER_HOST` and `DOCKER_CONTEXT`, or to run `docker context use default`. | Yes. |
| `not to a unix socket` | Docker is set to a daemon on another machine; the tool needs the local one. | Ask the user to point Docker at the local daemon's socket. | Yes. |
| `docker's buildx plugin, which builds the images, is not installed` | The images cannot be built without it. | Ask the user to install it (the `docker-buildx-plugin` package, or Docker Desktop). | Yes. |
| `building <image> failed` | An image (`dta-toolkit:<version>` or `dta-runtime:<version>`) could not be built. The lines before are the end of docker's output; the line names the log that holds all of it. | Read those lines. A host that cannot be reached (a timeout, a name that does not resolve, a refused connection): ask the user for network access to the hosts that "The first command on a machine" lists, then run the command again. `no space left on device`: ask the user to free about 2 GB of disk. Anything else: show the user the last lines and the log's path, and stop. | Yes. |
| `another dta call has been building the images for` | Another call has held the build's lock for an hour. | Tell the user: they stop that call, or, if none runs any more, remove the lock the line names. | Yes. |
| `is not present, and its sources are not beside the launcher` | The plugin is installed without the files its images are built from. | Ask the user to install the plugin again, whole. | Yes. |
| `is not present, and it is not built since DTA_TOOLKIT_IMAGE names it` | `DTA_TOOLKIT_IMAGE` names an image that is not on this machine. | Ask the user to build or pull that image, or to unset the variable. | Yes. |
| `the images cannot be built` | There is no folder for the build's log and lock: neither `XDG_STATE_HOME` nor `HOME` is an absolute path, or the folder cannot be created. | Ask the user to set `HOME`, or to make the folder the line names writable. | Yes. |
| `DTA_REBUILD` | The variable is set to something other than 1, or together with `DTA_TOOLKIT_IMAGE`. | Run the command without `DTA_REBUILD`: it is for someone who changes the tool's own sources. | No. |

## Gates

`dta status` lists every gate in `result.gates`, with its `result` and `reasons`. A gate that is `stale` was recorded for a manifest that has changed since: run the command that records it again.

| Gate | It fails when | What to do | Ask the user? |
|---|---|---|---|
| `intake` | `twin.id`, `contract.purpose`, or a source's `connector` or `location` has no value, or no source is declared. | Write what the reason names ([stage 0](stage-0-intake.md)). | Yes, for the purpose and the sources. |
| `contract` | A user-owned contract field other than a tolerance or staleness is not confirmed. | Ask for it or show it ([stage 1](stage-1-contract.md)), then `dta manifest confirm <path>`. | Yes. |
| `access` | `dta source probe` failed, never ran, or the way a source is reached changed since (`stale`). | `dta source probe` ([stage 2](stage-2-access.md)). | If a source cannot be reached. |
| `profiling` | A source has no `profile`. | `dta profile` ([stage 3](stage-3-profiling.md)). | No. |
| `design` | `dta manifest validate` found an error, never ran, or the manifest changed since (`stale`). | `dta manifest validate`; fix each error finding. | Only for what the finding's row says. |
| `gaps` | A required field is open, an error finding remains, or the contract is not signed or changed since signing. | [Stage 5](stage-5-gaps.md). | Yes, to confirm and to sign. |
| `build` | A build step failed, or the manifest changed since the build (`stale`). | `dta build` ([stage 6](stage-6-build.md)). | Only for what the step's row says. |
| `acceptance` | A check failed, or the manifest changed since (`stale`). | `dta accept` ([stage 7](stage-7-acceptance.md)). | Per check. |
| `handover` | `dta install-skill` refused, ran with `--provisional`, or the manifest changed since (`stale`). | `dta install-skill` ([stage 8](stage-8-handover.md)). | No. |

## Workspace and manifest

| Code | Meaning | What to do | Ask the user? |
|---|---|---|---|
| workspace.missing | The directory is not a workspace. | At stage 0: `dta init`. Later: check the `-w` path. | If you do not know the path. |
| workspace.locked | Another `dta` command holds the workspace (a build, a profile). | Wait until it ends, then run the command again. Never delete the lock. | No. |
| workspace.outside | A path in the manifest points outside the workspace. | Write the path relative to the workspace, inside it. | No. |
| workspace.unwritable | The workspace directory cannot be created or written. | Choose another directory for `dta init`. | Yes: where to put it. |
| workspace.bad_state | The tool's state under `.dta/` is unreadable. | Show the message to the user and stop; never edit `.dta/`. | Yes. |
| init.bad_id | The twin id is not lowercase letters, digits and hyphens. | Choose another id, such as `cold-store`. | No. |
| manifest.syntax | `manifest.yaml` is not valid YAML. | Fix the indentation or quoting at the place the message names. | No. |
| manifest.schema | A field is unknown, of the wrong type, or a value is not allowed. `error.detail.problems` lists each with its path. | Fix each path, using [manifest](manifest.md). | No. |
| manifest.unreadable | `manifest.yaml` cannot be read. | Check that it exists in the workspace. | No. |

## Environment and Docker

| Code | Meaning | What to do | Ask the user? |
|---|---|---|---|
| env.missing | A variable the manifest declares is missing from `.env` or empty. `error.detail.missing` names them. | Ask the user to fill those names in `.env`; never ask for the values. | Yes. |
| env.permissions | Others can read `.env`. | `chmod 600 <workspace>/.env`. | No. |
| env.unreadable | `.env` cannot be read. | Ask the user to check that it exists in the workspace and is theirs. | Yes. |
| env.bad_value | A port variable in `.env` is not a port number. | Ask the user to correct the named variable. | Yes. |
| config.missing_env | A variable a part of the twin needs is not set. | As `env.missing`; also check that it is declared under `env` and, for a source, in `sources.<source>.env`. | Yes, to fill it. |
| config.bad_value | A value in `.env` is not allowed, for example an API token shorter than 24 characters. | Ask the user to correct the named variable. | Yes. |
| stack.docker_unavailable | Docker is not running or not reachable. | Ask the user to start Docker, then run the command again. | Yes. |
| stack.env_missing | `.env` does not set a variable the stack needs. | As `env.missing`. | Yes. |
| stack.command_failed | A `docker compose` command failed; `error.detail` ends with its error output. | Read it. A port already in use: ask the user to choose another port in `.env`. An image that cannot be pulled: see `image.runtime_missing`. | Depends on the output. |
| stack.timeout | A container command did not finish in time. | `dta logs --tail 100`; run the command again once. | If it repeats. |
| image.runtime_missing | The twin's runtime image is not on this machine. | If `error.detail.runtime_image` is the default of `runtime.image` ([manifest](manifest.md)), the launcher builds it: run `dta status` once (a build of several minutes, see "The launcher" above), then the failed command again. Another image: tell the user it must be installed; continue once they say it is. | If it is not the default image. |
| runtime.too_old | The image named by `runtime.image` reads a lower manifest version than the manifest states; `error.detail` gives both numbers and the image. | If `runtime.image` is an older default, set it to the current default ([manifest](manifest.md)) and run the command again; the launcher builds the image when it is absent. With an image of the user's own, they rebuild it. | Only for an image of their own. |
| image.requirements_missing | `runtime.worker_requirements` names a file that does not exist. | Write it, or remove the field. | No. |
| image.build_failed | The worker image could not be built from the requirements; `error.detail.pip` holds the installer's output. | Fix the package names or versions in the requirements file. | No. |

## Sources and profiling

| Code | Meaning | What to do | Ask the user? |
|---|---|---|---|
| access.failed | At least one source could not be reached; `error.detail.failing` lists them, each with its own code. | Look up each source's code below. | Per code. |
| source.unavailable | The source did not answer, or not in time. | Check the location against [sources](sources.md), "Locations": a host on this machine is never `localhost`. Then ask the user whether it is running and reachable. | Yes. |
| source.auth_failed | The source refused the credentials. | Ask the user to check the variables named in `sources.<source>.env`; never ask for their values. | Yes. |
| source.rate_limited | The source asked for fewer requests. | Wait `error.detail.retry_after_s` seconds and run again; ask the user for the allowed rate and write `sources.<source>.rate_limit_period`. | Yes, for the rate. |
| source.invalid_config | The source's entry cannot be used as written; the message names the option. | Fix it with [sources](sources.md), "Options". | No. |
| source.unknown_dataset | A dataset or table named in the options does not exist. | Correct the name; for `sql`, leave out `datasets` once to see every table in the profile. | If you cannot tell the right name. |
| source.read_failed | The source answered, but not as a table: wrong `records` path, wrong format, a file that is not CSV. | Fix the dataset's options. | No. |
| source.error | Any other connector failure. | Read the message; if it does not name a fix, show it to the user. | Yes. |
| profile.failed | At least one source could not be profiled; `error.detail.failed` maps each to its code and message. | Look up that code; the sources that were profiled are already written. | Per code. |
| profile.batch_open | A delegation batch is open; profiling would change the manifest under it. | Merge or abort the batch ([delegation](delegation.md)), then profile. | No. |

## Ontology

| Code | Meaning | What to do | Ask the user? |
|---|---|---|---|
| ontology.missing | No ontology file at `ontology.path`, or the field is empty. | Write the file ([ontology](ontology.md)) and set `ontology.path` to it, relative to the workspace. | No. |
| ontology.unreadable | The ontology file is not readable YAML. | Fix the YAML at the place named. | No. |
| ontology.unusable | The ontology does not load; `error.detail` names the class or property. | Fix it with [ontology](ontology.md). | No. |
| ontology.signature | The ontology's types differ from `ontology.type_signature`. | Show the user the changed classes; on agreement, `dta ontology check --write`, then `dta manifest confirm ontology.type_signature`. | Yes. |
| ontology.type_signature | The types changed after the user confirmed them. | As `ontology.signature`. | Yes. |
| ontology.unknown_type | A mapping, action or reference names a class the ontology lacks. | Correct the name, or add the class with the user's agreement. | If a class is added. |
| ontology.unknown_property | A reference names a property its class lacks. | Correct the name, or add the property. | No. |
| ontology.abstract_mapping | A mapping fills an abstract class. | Map onto one of the concrete classes the message lists. | No. |
| ontology.required_unmapped | No mapping fills a required property of a class; every row would be refused. | Map it, or, if the data does not hold it, make it optional with the user's agreement. | If made optional. |
| ontology.unmapped_observation | `contract.boundary.observations` lists a property no mapping fills. | Map it, or remove it from the boundary. | No. |
| ontology.role | A fixed property is mapped with a time column, or a property with `dta_role: state` without one (warning). | Separate the fixed and the timed properties into two mappings ([sources](sources.md), "Mappings"). | No. |
| ontology.unit | A mapped property has a unit but its mapping states none, or the other way round (warning). | Add or remove `unit` in the mapping. | No. |
| ontology.function_param | A function parameter names a property that does not exist or is not numeric where it must be. | Correct the parameter ([functions](functions.md)). | No. |

## Manifest findings

`dta manifest validate` reports these in `result.findings` (exit 0, warnings only) or `error.detail.findings` (exit 1). Each has `code`, `severity`, `path` and `message`.

| Code | Meaning | What to do | Ask the user? |
|---|---|---|---|
| validate.failed | At least one finding is an error. | Look up each finding's code. | Per code. |
| ref.question_function | A question names a function not declared. | Declare it ([functions](functions.md)). | No. |
| ref.function_entry | A built-in's entry or a fixed parameter is not valid; or a registered entry is not `module:function`. | Correct it from [manifest](manifest.md), "Built-in functions". | No. |
| ref.code_declared | A registered function or model family is code, but `runtime.code` is not set. | Set `runtime.code` to the code directory. | No. |
| ref.model_family | A model candidate's family is not a shipped one. | Use a family from [manifest](manifest.md), "Model families". | No. |
| ref.question_model | A question names a model not declared, or one serving another question. | Name the model after the question ([models](models.md)). | No. |
| ref.model_question | A model names a question not declared. | Correct `models.<question>.question`. | No. |
| ref.control_action | A control names an action not declared, or one carrying out another control. | Correct the action or its `control`. | No. |
| ref.loop | A loop names an undeclared control or policy. | Correct it. | No. |
| ref.objective_kpi | The objective names a function not declared. | Declare it ([functions](functions.md), "The objective's function"). | No. |
| ref.env_declared | A `${NAME}` is used but not declared under `env`. | Declare it under `env` ([stage 0](stage-0-intake.md)). | No. |
| ref.env_unused | A variable is declared but nothing uses it (warning). | Remove it, unless the user will use it later. | No. |
| ref.clock_source | `runtime.clock` names a source that does not exist or tells no time. | Set `runtime.clock: wall`, or give that source a `clock` option. | No. |
| ref.incremental | An incremental dataset or its column is not in the profile. | Correct the name; profile again if the source changed. | No. |
| ref.property_format | A property reference is not written `Type.property`. | Correct it. | No. |
| ref.mapping_columns | A mapping names a dataset or column the profile does not hold. | Use the names from `sources.<source>.profile.datasets`. | No. |
| control.bounds | A control's bounds give no minimum, maximum or allowed values, or a minimum above the maximum. | Ask for the bounds ([question bank](question-bank.md)). | Yes. |
| control.binding | A supervised or autonomous control's action is not `binding: executor`. | Give the action an executor ([live twins](live.md)), or ask whether the control should be advisory. | Yes, if it becomes advisory. |
| action.executor_addon | An action has `binding: executor` but `contract.addons.executors` is not `true`. | Ask the user whether the twin may call the real system. | Yes. |
| action.reserved_header | An executor's `headers` set `Idempotency-Key`, which the twin sends itself with the id of the action's record. | Remove that header, and any parameter added only to fill it. | No. |
| ontology.action_precondition | An action's `precondition` is not clauses of the form `<property>:<op>:<value>` joined by ` and `, compares a property with a value it cannot hold, or the action has neither `object_type` nor `control`. | Rewrite the clause named in the message as [functions](functions.md) "Filters" says. | No. |
| control.on_failure_missing | An autonomous control whose action calls the real system has no `on_failure`. | Ask the fail-safe question of the [question bank](question-bank.md) and write the answer. | Yes: the choice is theirs. |
| control.on_failure_invalid | What `on_failure` names cannot be sent: the action does not exist, has no executor, acts on another class, is the control's own action under `release`, a parameter has no constant, or a constant is outside the action's or the control's limits. | Correct it as the message says ([live twins](live.md)). | Only if the value or the choice changes. |
| manifest.too_new | `manifest_version` is higher than this toolkit reads (`error.detail.reads`). | The manifest was written with a newer plugin: update the plugin; do not lower the number. | Yes: they update the plugin. |
| manifest.version_too_low | A field is set that needs a higher `manifest_version` than the manifest states. | Set `manifest_version` to the number the message names. | No. |
| manifest.version_unset | Warning: the manifest has no `manifest_version` and is read as the current one. | Write `manifest_version` with the number the message names. | No. |
| tier.static_control | A static twin has a control that is not advisory. | Ask: advisory, or a live twin? | Yes. |
| tier.static_extras | A static twin declares settings it never uses (warning): `fidelity`, `control_loops`, or `sync.minimum_interval` and `sync.maximum_interval` when nothing forecasts. | Remove exactly what the message names; keep `sync.interval`, which a static twin with forecasts needs. | No. |
| sync.mapping_invalid | The build's `load` step would refuse a view or mapping as written; the message says how to write it. Also raised by the build itself, at step `load`. | Do what the message says ([sources](sources.md), "Views" and "Mappings"): name datasets as `"<source>"."<dataset>"`; use `snapshot` only for the one mapping of a class, without `valid_time`; give one of `column` and `expression`. | No. |
| grid.no_interval | A twin with a forecast question or a model has no `sync.interval`, in a static twin too; models step on it. | `dta sync derive --write` ([live twins](live.md), "The sync interval"). | No. |
| grid.horizon | A forecast question's horizon is not a whole multiple of `sync.interval`. | `dta sync derive --write` again, which fits the interval to the horizons; if it fails with `sync.infeasible`, see that row. | No. |
| grid.backtest_step | A model's `backtest.step` is not a whole multiple of `sync.interval`. | Set the step to the question's horizon, or the multiple the message proposes ([models](models.md), "Size the backtest from the history"). | No. |
| secret.inline | A location, executor address or header holds a literal credential. | Replace it with a `${NAME}` placeholder, declare the name, and ask the user to put the value in `.env`. | Yes, to fill `.env`. |
| function.interval | A function with `trigger: interval` has no `interval`. | Add it. | No. |
| status.stale | A `field_status` entry names a field that no longer has a value (warning). | Remove that line from `field_status`. | No. |
| coverage.unmapped_target | A question's target is not filled by any mapping (warning). | Map it ([sources](sources.md)). | No. |
| feasible.noise | A tolerance is below the measured noise of its data. | Show the user the noise and propose a tolerance above it. | Yes. |
| feasible.staleness | A staleness is shorter than how often a source delivers. | Show the user the source's refresh period and propose a staleness above it. | Yes. |
| feasible.interval | The sync interval cannot meet a staleness, a refresh period, a rate limit or its limits. | `dta sync derive --write`; if it says the contract is infeasible, see `sync.infeasible`. | If infeasible. |
| feasible.history | A forecast question has less history than its backtest needs. | Shorten the backtest ([models](models.md)); if it cannot be shorter, ask whether the question may be provisional. | Yes, to make it provisional. |
| feasible.acceptance | A model's acceptance threshold is above its question's tolerance (warning). | Lower the threshold to the tolerance. | No. |
| check.internal | A check itself failed. | Run the command again; if it repeats, show the message to the user. | If it repeats. |

## Confirm, sync derive and sign

| Code | Meaning | What to do | Ask the user? |
|---|---|---|---|
| confirm.refused | No path was confirmed; `error.detail.refused` gives each path's own code. | See the next rows. | Per code. |
| confirm.not_user_field | The path is agent-owned; it is confirmed by being written. | Leave it out. | No. |
| confirm.no_value | The path has no value yet. | Ask for it, write it, then confirm. | Yes. |
| confirm.not_required | The path is not a required field: a container such as `contract.controls.<control>.bounds`, or an optional field. | Confirm its leaves, or leave it out. | No. |
| confirm.nothing | Nothing was named to confirm. | Name the paths. | No. |
| sync.interval_confirmed | `sync.interval` was written by hand, so the rule is not applied. | Delete `interval` under `sync`, then `dta sync derive --write`. | No. |
| sync.underivable | No input of the rule is known: no source has a refresh period, no question a staleness and no forecast a horizon. | Run `dta profile`; if the profile measures no refresh period (data without timestamps), ask the user how often the source delivers and write `sources.<source>.refresh_period`. | If the profile cannot measure it. |
| sync.infeasible | No interval meets every staleness and horizon given how the sources deliver, or the interval lies outside `sync.minimum_interval` or `sync.maximum_interval`. | Show the user which question and source conflict; ask for a longer staleness, or a horizon that is a whole multiple of the delivery period the message names. Outside a limit you wrote: remove the limit. | Yes, for a staleness or horizon. |
| sync.batch_open | A delegation batch is open. | Merge or abort it first. | No. |
| sign.gaps | Required fields are still open. | [Stage 5](stage-5-gaps.md), step 1. | Yes. |
| sign.errors | Error findings remain. | `dta manifest validate`; fix them. | Per finding. |
| sign.changed | The manifest changed while signing. | Show the summary again and ask again. | Yes. |
| sign.declined | The user typed something other than the twin's id when signing in their terminal. | Ask what they want changed. | Yes. |
| sign.not_interactive | No terminal for the interactive sign. | Use `dta manifest sign --non-interactive --approved-by "<user's name>"`, only after the user's yes. | Yes, the yes. |
| sign.approved_by_missing | `--non-interactive` without `--approved-by`. | Add the user's name. | No. |

## Build

| Code | Meaning | What to do | Ask the user? |
|---|---|---|---|
| build.not_ready | A gate before the build has not passed; `error.detail.gates` and `error.detail.next`. | Do what `next` says. | Per gate. |
| build.failed | A step failed; `error.detail.step`, `error.detail.evidence`. | The table below, by step. | Per step. |
| build.bad_step | `--from` names no step. | Use one of the steps listed in the message. | No. |
| twin.no_interval | The twin has no `sync.interval`, so its models have no grid (409 from the API, at step `models`). `dta manifest validate` reports it first as `grid.no_interval`. | `dta sync derive --write`, `dta manifest validate`, then `dta build --from models`. | No. |
| model.bad_params | A model's settings do not fit its grid or family: a backtest step that is not a whole multiple of `sync.interval`, a parameter a family does not take, or a `season_length` it cannot use. The `models` step skips that model. | Read the message; fix `models.<question>` ([models](models.md)); `dta manifest validate`; `dta build --from models`. | No. |
| model.none_active | A forecast was asked for, but its question has no accepted model (409; often in acceptance, under `questions.answered`). | `dta models list`: if the model was skipped, fix it as its error code says; if no candidate was accepted, [stage 7](stage-7-acceptance.md), "A question outside its tolerance". | If no candidate was accepted. |

| Step | Usual cause | What to do | Ask the user? |
|---|---|---|---|
| `scaffold` | `.env` lacks a variable; the message names it. | Ask the user to fill it. | Yes. |
| `image` | See the `image.*` codes. | As the code says. | Per code. |
| `store` | The store container did not become healthy: a port in use (with read-only SQL, `DTA_SQL_PORT` too) or no disk space; or the reader of read-only SQL could not be granted (`evidence.code` `addons.*`). | `dta logs store --tail 100`; tell the user what it says. An `addons.*` code: as "Add-ons" below. | Yes. |
| `install` | The ontology and the manifest were not accepted by the store. | Read `evidence.message`; `dta ontology check`; `dta manifest validate`. | No. |
| `load` | The load run did not succeed or wrote no data. `evidence` holds `rows_read`, `rows_rejected` and each source's outcome. | A source error: as its code. All rows rejected: compare the mappings' identity, required properties and units with the profile. No rows read: check the dataset names and filters. Then `dta build --from load`. | If a source fails. |
| `api` | The API container did not start. | `dta logs api --tail 200`. | If a port is in use. |
| `contract` | The API did not answer its own requirements. | `dta logs api --tail 200`; `dta build --from api`. | If it repeats. |
| `sync` | A live twin's scheduler or clock did not start. | `dta logs scheduler --tail 200`; check `runtime.clock`. | No. |
| `code` | The code directory is missing or the worker did not start. | Check `runtime.code`; `dta logs worker --tail 200`. | No. |
| `functions` | A function was refused when registered; `evidence.code` is a `function.*` code. | Fix its entry or parameters ([functions](functions.md)). | No. |
| `models` | An evaluation failed or timed out. | `dta models list`; `dta logs worker --tail 200`; a long history may need `--models-timeout`. | No. |
| `questions` | The measurement of the questions failed. | `dta questions measure`; read its error code. | No. |
| `ui` | The UI did not answer. | `dta logs ui --tail 100`. | If a port is in use. |
| `mcp` | The MCP adapter did not answer on `DTA_MCP_PORT`. | `dta logs mcp --tail 100`; a port in use: ask the user for another `DTA_MCP_PORT`. | If a port is in use. |
| `skill` | The operator skill could not be generated. | Read `evidence.message`. | No. |
| `sdk` | The Python client could not be generated; `evidence.code` is an `sdk.*` code. | As the code says ("Add-ons" below). | Per code. |

## Add-ons

What each add-on builds, and the variables it needs: [add-ons](addons.md).

| Code | Meaning | What to do | Ask the user? |
|---|---|---|---|
| addons.env_undeclared | A selected add-on needs a variable that `env` does not declare; the finding's message names it. | Declare it ([add-ons](addons.md)); `dta manifest validate`. | No. |
| addons.reader_password_missing | `.env` has no `DTA_SQL_READER_PASSWORD`. | `dta env template` generates it. | No. |
| addons.reader_password_unusable | The reader's password in `.env` is shorter than 16 characters, or holds a quote, a backslash or a blank. | Ask the user to delete that line of `.env`, then `dta env template`. | Yes. |
| sdk.not_selected | `dta generate sdk` while `contract.addons.sdk_python` is `false`. | Nothing, unless the user wants the client: stage 1 and stage 5 again. | Yes, whether they want it. |
| sdk.api_unavailable | `dta generate sdk` could not read the API's description. | `dta up`, then run it again. | No. |
| sdk.api_unexpected | The API's description lacks an operation or parameter the client needs. | Show the message to the user and stop; the twin works without the client. | Yes. |
| sdk.client_failed | The client's generator failed. | As `sdk.api_unexpected`. | Yes. |
| mcp.tools_mismatch | The MCP adapter's tools differ from the API's operations; it does not start (`dta logs mcp`). | Show the message to the user: the runtime image is inconsistent. | Yes. |

## Export and import

| Code | What it means | What to do | Ask the user? |
|---|---|---|---|
| export.store_not_running | The twin's store is not running, so there is nothing to back up. | `dta up`, then `dta export` again. | No. |
| export.not_installed | The store runs but holds no twin yet. | Build the twin first (`dta build`). | No. |
| export.api_missing | The API container, which holds the twin's artifacts, does not exist. | `dta up`, then `dta export` again. | No. |
| export.store_busy | The twin kept committing during five backups in a row. | Run `dta export` again; if it repeats, tell the user. | If it repeats. |
| export.target_invalid | `--out` names something that cannot be written. | Give a folder that exists or a file name in one. | No. |
| import.bundle_not_found | The bundle file does not exist. | Check the path; the user may have to copy the file to this machine. | Yes. |
| bundle.unreadable | The file is not a twin bundle or is damaged. | Ask the user for the file again. | Yes. |
| bundle.unsafe_member | The bundle holds a path that would be written outside the workspace. | Do not import it; tell the user. | Yes. |
| bundle.checksum_mismatch | A member of the bundle is not what the bundle's own list says: the file changed after export. | Ask the user to copy or export it again. | Yes. |
| bundle.unknown_format | The bundle was written by a newer `dta`. | The user updates the plugin, or exports with this version. | Yes. |
| import.target_exists | The target folder already holds a workspace or a bundle. | Choose an empty folder with `--into`. | No. |
| import.store_volume_exists | A stack of this twin's name already has a store on this machine. | Never add `--replace-data` yourself: it deletes that store. Tell the user what exists and let them decide. | Yes. |
| import.bundle_inconsistent | The bundle's parts do not belong together. | Ask the user to export again. | Yes. |
| import.head_mismatch | The restored store's newest version is not the exported one. The twin was not imported. | Tell the user the branch and both ids from the message; do not build on it. | Yes. |

## Acceptance and handover

| Code | Meaning | What to do | Ask the user? |
|---|---|---|---|
| accept.failed | At least one check failed. | [Stage 7](stage-7-acceptance.md), step 2. | Per check. |
| accept.not_built | The build gate has not passed for this manifest. | `dta build`. | No. |
| handover.not_accepted | A gate before handover has not passed. | Go back to that stage. | No. |
| skill.missing | The operator skill has not been generated. | `dta build --from skill`. | No. |
| skill.stale | The operator skill was generated from another manifest. | `dta build --from skill`, then `dta accept`. | No. |
| install.options | `--scope` and `--target` were both given. | Give one. | No. |

## Delegation

Submit and merge refusals list several reasons in `error.detail`; fix every one.

| Code | Meaning | What to do | Ask the user? |
|---|---|---|---|
| tasks.batch.open | A batch is already open. | Merge or abort it first. | No. |
| tasks.batch.none | No batch is open. | `dta tasks plan --stage <stage> --open`. | No. |
| tasks.batch.nothing | The stage has no task left to open. | Do the stage's remaining work yourself. | No. |
| tasks.stage.required | The build is at a stage with nothing to delegate. | Pass `--stage 3`, `--stage 4` or `--stage operate`, or work alone. | No. |
| tasks.lease.held | Another holder has the task. | Give the holder another task. | No. |
| tasks.lease.expired | The lease ran out before submitting. | Renew it with `--renew --token`, then submit again. | No. |
| tasks.lease.token | The token is not the current one. | Use the token of the latest claim. | No. |
| tasks.lease.none | The task is not claimed, so there is nothing to renew. | Claim it. | No. |
| tasks.claim.closed | The task is already submitted, merged or abandoned. | Leave it. | No. |
| tasks.submit.refused | The submit was checked and refused; each reason has its own code. | Fix each reason, submit again. | No. |
| tasks.merge.refused | A task cannot be merged; each reason has its own code. | Fix each reason, or abort that task. | No. |
| tasks.merge.not_submitted | A task of the batch is not submitted yet. | Wait for it, or abort it. | No. |
| tasks.result.missing | `output/result.json` does not exist. | Write it. | No. |
| tasks.result.invalid | `result.json` is not an object with exactly the four keys, or is too large. | Correct it ([delegation](delegation.md), "The holder's brief"). | No. |
| tasks.patch.not_owned | The patch sets keys the task does not own. | Move those changes into `proposals`. | No. |
| tasks.patch.schema | With the patch applied, the manifest breaks its schema. | Correct the patched values. | No. |
| tasks.artifact.undeclared | `output/` holds files `artifacts` does not list. | List them or delete them. | No. |
| tasks.zone.violation | Something under `tasks/` changed that no task may change. | Abort the batch and start again. | No. |
| tasks.pin.manifest | Manifest values the task reads changed after the batch opened. | Abort the task, then plan again. | No. |
| tasks.pin.ontology | The ontology file changed after the batch opened. | Abort the batch, then plan again. | No. |
| tasks.pin.owned | Keys the task owns were written by someone else after the batch opened. | Abort the task, then plan again. | No. |
| tasks.foreign.modified | A shared file (`manifest.yaml`, the ontology, `discussion.md`) was edited by hand while the batch was open. | Restore the file as it was, or abort the batch; make the edit after merging. | No. |
| tasks.foreign.added | A file appeared in the workspace outside `tasks/` while the batch was open. | Move it away until the batch is merged, or abort the batch. | No. |
| tasks.foreign.removed | A shared file was removed while the batch was open. | Restore it, or abort the batch. | No. |
| tasks.ledger.unreadable | The tool can no longer tell sanctioned changes from others. | `dta tasks abort --batch`. | No. |
| tasks.files.unsettled | Shared files changed while being read. | Run the command again. | No. |
