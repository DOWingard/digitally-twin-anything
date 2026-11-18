# Stage 3: profiling

Goal: a measured profile of every source: its datasets, their columns and keys, the time span of their history, how often new rows arrive, and how noisy each signal is. The design of stage 4 and the feasibility checks rest on these numbers, not on guesses.

Gate `profiling` is computed from the manifest. It passes when every source has `sources.<source>.profile`.

## 1. Profile

If you work with other agents, `dta tasks plan --stage 3` lists one task per source; see [delegation](delegation.md). Otherwise:

```sh
dta profile
```

It reads each source from inside the stack (read-only), and writes into `manifest.yaml`, marked as derived: `sources.<source>.profile`, and `sources.<source>.refresh_period` when the source has timestamped rows.

- Exit 0: `result.written` lists the sources profiled. Go on.
- Exit 1, code `profile.failed`: `error.detail.failed` maps each source that could not be profiled to its code and message; look the code up in [troubleshooting](troubleshooting.md), fix, and run `dta profile --source <source>` for that source.
- Exit 1, code `profile.batch_open`: a delegation batch is open; finish or abort it first ([delegation](delegation.md)).
- A very large dataset: `dta profile --row-cap 200000` computes the statistics on that many rows (`sampled_rows` in the profile says so).

Never edit a profile by hand: it is a measurement.

## 2. Read the profile

For each source, under `sources.<source>.profile.datasets.<dataset>`, note:

| What | Where | Used for |
|---|---|---|
| Rows | `rows` | Whether the dataset holds what the user expects. |
| Columns, their types and null shares | `columns.<column>.dtype`, `columns.<column>.null_fraction` | Mappings and ontology property types (stage 4). |
| Candidate keys | `candidate_keys` | `identity` of a mapping. |
| The time column and the series columns | `time_column`, `entity_columns` | `valid_time` and `identity` of a timed mapping. |
| History span | `history_start`, `history_end` | Whether forecast questions have enough history ([models](models.md)). |
| Delivery period | `refresh_period` (dataset) and `sources.<source>.refresh_period` | Staleness and the sync interval ([live twins](live.md)). |
| Noise floor | `columns.<column>.noise_sigma` | The smallest tolerance that can be met. |
| Change time | `columns.<column>.change_time` | The sync interval. |

## 3. Revisit tolerances and stalenesses with the user

The user stated each question's tolerance and staleness in stage 1, before anything was measured. Now compare, for each question:

- Tolerance against noise: if `contract.questions.<question>.tolerance.value` is below the `noise_sigma` of a column its targets are read from, the twin cannot meet it: the check `feasible.noise` will refuse it in stage 4.
- Staleness against delivery (live twins): if `contract.questions.<question>.staleness` is shorter than the `refresh_period` of a source its targets come from, no sync can meet it: the check `feasible.staleness` will refuse it.

For each conflict, tell the user the stated value, the measured one and what it means, and propose a value that can be met (at least the noise floor; at least twice the delivery period). Example: "You asked for answers within 0.1 °C, but the sensors themselves scatter by 0.3 °C. I propose a tolerance of 0.5 °C. Shall I use that?" Write what the user decides, record it in `discussion.md`, and leave it unconfirmed until stage 5.

If a question cannot be answered from what the sources hold (a column it needs is missing, or the history is shorter than a forecast needs), tell the user now, with the options: another source, a different question, or a question marked provisional.

## 4. Check the gate

```sh
dta status
```

- Gate `profiling` `passed`: go to [stage 4](stage-4-design.md).
- `failed`: the reasons name each source without a profile; profile it.
