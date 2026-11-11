# Storage engine benchmark

Decides which open source store holds a typed graph together with its full
version history. Two candidates were tested: Dolt 2.7.3 and TerminusDB 12.0.9.

**Result: Dolt.** It passed every gate on both full-size fixtures. TerminusDB
passed at toy scale and failed at full size.

## What the store has to do

1. Hold typed objects and typed links, and refuse writes that break the schema.
2. Keep every committed version, and read any object or table as of any version.
3. Return the history of one property across all versions.
4. Diff two versions.
5. Traverse links, at any version.
6. Branch from any version, write on the branch, and leave the live line untouched.
7. Keep serving a reader pinned to an old version while new versions are committed.
8. Hand back whole tables in columnar form.

## How it is tested

- One schema, written in LinkML (`ontology/fixture.yaml`), is translated into
  each engine's own schema. Nothing engine-specific is written by hand.
- A deterministic generator builds a graph of sites, assets, sensors and
  asset-to-asset links, then evolves it one commit at a time. It keeps its own
  ground truth: full state at sampled versions and the complete change history
  of a few tracked assets.
- Each engine is loaded, **checked against the ground truth**, then timed.
- Pass and fail thresholds are constants in `spike/bench.py`, fixed before any
  measurement.

| Fixture | Assets | Sensors | Links | Commits | Changes per commit |
|---|---|---|---|---|---|
| smoke | 60 | 60 | 150 | 12 | 14 |
| wide | 20,000 | 20,000 | 60,000 | 200 | 440 |
| long | 1,000 | 1,000 | 3,000 | 2,000 | 24 |

## Gates

All latencies are p95, measured from the client.

| Gate | Limit |
|---|---|
| Every result matches ground truth | required |
| Point read as of a random version | 50 ms |
| Old versions no slower than new (oldest quarter / newest quarter) | ratio 3 |
| Whole table as of a version | 2 s |
| Property history of one object | 1 s |
| Two-hop traversal, natively or through an edge-table projection | 200 ms native, or 2 s projected |
| Branch creation | 1 s |
| Schema refuses bad enum, missing field, dangling reference, wrong type | required |

## Results

### Dolt

| Operation (p95 ms) | wide | long |
|---|---|---|
| Commit | 51.6 | 13.5 |
| Point read as of a version | 0.8 | 0.6 |
| Oldest / newest read ratio | 1.33 | 1.01 |
| Node table as of a version | 153 (20,000 rows) | 6.7 (1,000 rows) |
| Link table as of a version | 227 (60,000 rows) | 55 (3,000 rows) |
| Property history | 14.6 (68 points) | 115 (668 points) |
| Diff, adjacent versions | 18.5 | 1.5 |
| Diff, first version to head | 29.2 | 2.8 |
| Traversal, 1 / 2 / 3 hops | 1.8 / 7.0 / 4.5 | 1.4 / 1.4 / 2.2 |
| Traversal through a projection | 292 | 14.5 |
| Branch create / commit / delete | 5.5 / 17.5 / 5.4 | 5.1 / 18.1 / 5.2 |
| Read while another connection commits | 3.6 | 2.7 |

- Every correctness check passed on both fixtures.
- All five invalid writes were refused, including the range check.
- Initial load of 100,050 objects: 3.8 s. Whole wide run: 23 s. Whole long run: 25 s.

### TerminusDB

Smoke fixture: every correctness check passed. Wide fixture, stopped before completion:

| Operation | TerminusDB | Dolt |
|---|---|---|
| Load 200 commits | 150 s | 13 s |
| Point read | about 10 ms | 0.4 ms |
| Node table, 20,000 rows | about 1.3 s | 0.09 s |
| Link table, 60,000 rows | about 8.8 s | 0.22 s |
| Property history | no answer within about five minutes | 15 ms |

- The document-history endpoint returned "Connection timed out" during
  verification and again during measurement, which fails the property-history
  gate. The run was stopped there, so no TerminusDB numbers exist for diff,
  traversal or branching at this size, and the long fixture was not run on it.
- It has no range constraints, so the minimum-value rule was accepted.
- Its document interface writes upserts and deletes as separate commits, so one
  logical version becomes two.
- Responses from its document endpoint stall about 40 ms on a reused HTTP
  connection (50 ms kept alive, 11 ms on a fresh one). The adapter opens a
  connection per request so the engine is not penalised for this.

## What Dolt does not do by itself

- **It is relational.** An object type is a table, a link type is a table. The
  mapping from the ontology to tables is generated; LinkML's own
  `gen-sqltables --dialect mysql` also produces tables, keys, enums and foreign
  keys for this schema, without the range checks.
- **Traversal is recursive SQL.** Hop-limited neighbourhoods are fast. Shortest
  paths and pattern matching need a graph library over the link table, which the
  projection row above measures.
- **Disk use grows with commit count.** The long fixture took 332 MB after
  garbage collection for 2,017 commits on a 5,000-row database, about 170 KB per
  commit. At one commit a minute that is roughly 240 MB a day.
- **Untested here:** type inheritance, multi-valued properties, merges, and
  anything beyond 100,000 objects.

## Running it

Requires Docker and `uv`.

```
cp .env.example .env        # then set the two passwords
docker compose up -d
uv sync
uv run --env-file .env python -m spike --fixture smoke
uv run --env-file .env python -m spike --engine dolt --fixture wide long
docker compose down -v
```

- Results are written to `results/<engine>-<fixture>.json`, and a structured log
  to `results/run-<id>.log`.
- The process exits with an error if any variable in `.env.example` is unset.
- `SPIKE_LOG_LEVEL` (default `info`) controls log verbosity.

## Layout

| Path | Contents |
|---|---|
| `ontology/fixture.yaml` | The LinkML schema |
| `spike/ontology.py` | Loads the schema and translates it for each engine |
| `spike/fixture.py` | The evolving graph and its ground truth |
| `spike/engines/` | One adapter per engine behind a common interface |
| `spike/bench.py` | Load, verification, measurements and gates |
| `results/` | Result files from the runs reported above |
