# Question bank

Every question you may put to the user, by the manifest field it fills. Ask one at a time, always with the proposed default, in plain words without field names. Write the answer to the field at once and append the entry to `discussion.md`.

Value formats used below:

- A duration is ISO 8601: `PT15M` is 15 minutes, `PT1H` one hour, `P1D` one day, `P7D` seven days.
- A unit is a UCUM code: `m`, `m2`, `Cel` (degrees Celsius), `kW`, `%`, `L/min`, `1` for a plain count.
- A property is written `Type.property`, with the type in CamelCase and the property in snake_case: `Room.setpoint`.

## Intake questions

| Intake field | Ask | Default to propose | Write |
|---|---|---|---|
| `twin.name` | Not asked: take the system's name from the conversation. | | The name, as the user says it. |
| `sources.<source>.description` | "Where does the data about the system live: a database, an HTTP interface, files? Name each one." | The stores you found in what the user pointed at. | One source per store, with the user's words as description. |
| `sources.<source>.location` | "What is the address of `<store>`, without any password? I will name a variable for each secret part, and you write the value into the workspace's `.env` file yourself." | The address you found, with placeholders for secrets. | The address with `${NAME}` placeholders ([sources](sources.md), "Locations"). |

## Contract fields

Required whenever the "When it applies" column holds; the gate and the signature need every one of them.

| Contract field | When it applies | Ask | Default to propose | Write |
|---|---|---|---|---|
| `contract.purpose` | always | "In a sentence or two: what should the twin let you see or decide, and for whom?" | A sentence built from what the user said so far. | The user's sentence. |
| `contract.tier` | always | "Should the twin be loaded once from the data as it is now (static), or keep reading the sources and stay current (live)?" | `live` when a source keeps receiving data and the user wants the present; else `static`. | `static` or `live`. |
| `contract.questions.<question>.text` | per question | "What should the twin answer? Give it as a question." | A question built from the purpose (see stage 1). | The question, ending with `?`. |
| `contract.questions.<question>.kind` | per question | "Is this about what happened (descriptive), why or where something went wrong (diagnostic), what will happen (predictive), or what to do (prescriptive)?" | The kind its wording implies. | `descriptive`, `diagnostic`, `predictive` or `prescriptive`. |
| `contract.questions.<question>.priority` | per question | "Which questions matter most? I will rank them in the order you asked them unless you say otherwise." | The order of asking: 1, 2, 3. | A whole number, 1 the most important. |
| `contract.questions.<question>.tolerance.metric` | per question | "How should the error of an answer be measured: the average miss (mean absolute error), large misses weighted more (root mean square), the miss as a share of the value (percentage), the largest single miss, or the share of wrong items in a list?" | `mae` for a number; `error_rate` for an answer that lists objects. | `mae`, `rmse`, `mape`, `max_abs` or `error_rate`. |
| `contract.questions.<question>.tolerance.value` | per question | "How far off may an answer be before it misleads you? In `<unit>`." | A round number the user's decisions can live with. Say how it will be checked: a forecast's error is measured by backtest; for any other question the tolerance is compared only with the measured noise of the data it reads, where profiling has one, and otherwise is never measured (the question is then validated on its answer time alone). | A number greater than 0, in the unit of the targets (`error_rate` as a fraction: `0.05`). |
| `contract.questions.<question>.staleness` | per question | "How old may the newest data behind an answer be?" | `P1D` for a static twin; for a live twin, twice the time between two deliveries of the source. | A duration. |
| `contract.controls.<control>.description` | per control | "What should the twin be able to set in the real system?" | None: ask only if the user mentioned acting. | The user's words. |
| `contract.controls.<control>.target` | per control | "Which value does that change, on which kind of object?" | A `Type.property` you propose now; the ontology of stage 4 must use exactly these names. | `Type.property`. |
| `contract.controls.<control>.bounds` | per control | "Which values may the twin set: a lowest, a highest, a list of allowed values, and the largest change in one step?" | The range the user named; there is no safe default, so ask until at least a lowest, a highest or a list is given. | At least one of `minimum`, `maximum`, `allowed`; `max_step` when given. The bounds have no confirmation of their own (`dta manifest confirm` answers `confirm.not_required`): record the user's answer in `discussion.md`; the signature in stage 5 covers them. |
| `contract.controls.<control>.autonomy` | per control | "Should the twin only recommend (advisory), act after a person approves each action (supervised), or act on its own (autonomous)?" | `advisory`; `supervised` only when the user asked the twin to act. A static twin is always `advisory`. | `advisory`, `supervised` or `autonomous`. |
| `contract.controls.<control>.on_failure.mode` | per autonomous control whose action calls the real system | "If the twin can no longer vouch for itself, the real system is left with the last thing the twin set. Should the twin then send nothing (hold), hand the object back to its own local control (release), or send one value that is safe in every situation (safe value)? This works only while the twin is running: it does not replace a watchdog in your own system." | None: there is no safe default. Say what you would choose and why: `release` when the system has a call that hands control back, `safe_value` when one value is safe whatever the state, `hold` when the system protects itself. | `hold`, `release` or `safe_value`. |
| `contract.objective.description` | a prescriptive question or a control exists | "What outcome should the twin's advice and control improve?" | The outcome the purpose names. | The user's words. |
| `contract.objective.direction` | a prescriptive question or a control exists | "Is less of it better, or more?" | The direction the wording implies. | `minimize` or `maximize`. |
| `contract.objective.constraints.<constraint>.description` | per constraint the user names | "Is there a limit that must hold while the twin improves that, whatever the gain?" | None: write a constraint only when the user names one. | The limit, in the user's words. |
| `contract.notification.kind` | the tier is `live` | "When the twin finds it no longer matches the real system, should it only write that to its log, or also call a webhook (a chat channel, an alerting service)?" | `log`. | `log` or `webhook`. |
| `contract.notification.webhook_env` | the notification kind is `webhook` | "I will name a variable for the webhook address; please write the address into the workspace's `.env` yourself." | `<ID>_ALERT_URL`, with the twin id in capitals. | The variable's name; also add `env.<NAME>` with `secret: true`. |

## Optional contract fields

None of these is a question of its own. Each row says when to write it; leave every other one out, and its default applies.

| Optional contract field | Ask | Default to propose | Write |
|---|---|---|---|
| `contract.cadence_preference` | Not asked. Write it only when the user states an interval of their own: it is recorded, but the sync interval is always derived from the sources and the stalenesses (see [live twins](live.md)). | Not set. | A duration. |
| `contract.questions.<question>.tolerance.unit` | Not asked separately: the unit the tolerance value is in. | The unit of the question's targets. | A UCUM code. |
| `contract.controls.<control>.bounds.minimum` | Part of the bounds question. | | A number. |
| `contract.controls.<control>.bounds.maximum` | Part of the bounds question. | | A number. |
| `contract.controls.<control>.bounds.allowed` | Part of the bounds question. | | A list of values. |
| `contract.controls.<control>.bounds.max_step` | Part of the bounds question. | | A number greater than 0. |
| `contract.controls.<control>.on_failure.action` | Part of the fail-safe question, for `release`: which call hands the object back. Declare it as an action with `binding: executor` on the control's class ([live twins](live.md)). | For `safe_value`, the control's own action. | An action id. |
| `contract.controls.<control>.on_failure.parameters` | Part of the fail-safe question: for `safe_value`, "Which value is safe?"; for `release`, whatever the hand-back call needs. Every parameter of the action gets a constant. | | A map of parameter names to constants; a safe value is `value: <constant>` inside the control's bounds. |
| `contract.controls.<control>.on_failure.after_stale` | "Should the same happen when the twin has had no fresh data for some time, and after how long?" | Not set: only a failed twin sends it. | A duration. |
| `contract.addons.executors` | Asked with each control that is `supervised` or `autonomous`: "The twin will call `<system>` to carry out approved actions. Is that allowed?" | `false`; `true` only when the user said yes. | `true` or `false`. It is optional, so it cannot be confirmed (`confirm.not_required`): leave it out of `dta manifest confirm` and record the user's answer in `discussion.md`. The summary the user signs in stage 5 lists it under "Add-ons", and the signature covers it. |
| `contract.addons.sdk_python` | Part of the add-ons question: "Do you want a Python package for your own code, with a typed accessor for each kind of object and a method for each question?" | `false`. | `true` or `false`. Optional, so it cannot be confirmed: record the answer in `discussion.md`; the signed summary lists it under "Add-ons". What it builds: [add-ons](addons.md). |
| `contract.addons.mcp` | Part of the add-ons question: "Should AI agents reach the twin as MCP tools, on a port of this machine?" | `false`. | `true` or `false`, recorded as `contract.addons.sdk_python`. When `true`, declare `DTA_MCP_PORT` under `env` ([add-ons](addons.md)). |
| `contract.addons.llm_extraction` | Not asked: recorded only, nothing is built for it. Write it only when the user asks for documents to be re-read by a language model, after telling them so. | `false`. | `true` or `false`. |
| `contract.addons.readonly_sql` | Part of the add-ons question: "Do you want to read the twin's data and history with SQL, as a user that can only read, on a port of this machine?" | `false`. | `true` or `false`, recorded as `contract.addons.sdk_python`. When `true`, declare `DTA_SQL_PORT` and `DTA_SQL_READER_PASSWORD` under `env` ([add-ons](addons.md)). |
| `contract.hosting` | Not asked: the twin runs on this machine. | `local`. | `local`. |

Of the add-ons, `contract.addons.llm_extraction` is recorded only: nothing is built for it. Write it only when the user asks for documents to be re-read by a language model, after telling them so. The others change what is built.

## Outside the contract

The user also owns `ontology.type_signature`: the object and link types of the twin. It is not asked as a question; in stage 4 you show the user the types you designed and confirm the signature once they agree ([stage 4](stage-4-design.md), "Agree the types").
