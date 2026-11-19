# Sources and mappings

A source is one place the twin reads data from: a folder of files, a database or an HTTP interface. Every source is read only, from inside the twin's containers, with the connector its `connector` field names. A mapping says how the rows of one dataset of a source become objects of one ontology class.

## Which services are sources

- A service the twin only sends commands to (a control interface that switches a pump or sets a setpoint) is not a source: it is the address of an action's executor ([live twins](live.md), "The executor"). It becomes a source only if it also serves data a question, control or required property needs.
- A service or feed whose data no question, control target or required property needs is left out. Tell the user which ones you leave out and why, and record it in `discussion.md`.

## Connector kinds

| The data is in | `sources.<source>.connector` | Reads |
|---|---|---|
| CSV, Parquet or JSON Lines files in a folder on this machine, or in a folder a web server lists | `file` | Each dataset is a file name pattern inside the folder. |
| A PostgreSQL, MySQL or MariaDB database | `sql` | Each dataset is a table or one SELECT statement. |
| An HTTP interface that answers JSON | `http` | Each dataset is one endpoint, with its paging. |

The kind `custom` is accepted by the manifest but cannot be opened by the tool: do not use it. If a source fits none of the three kinds, tell the user which part cannot be read and ask whether the data can be exported to files.

## Locations

`sources.<source>.location` is the source's address. Put every secret part, and every part that differs between machines, in a `${NAME}` placeholder, and name each variable in `sources.<source>.env` and under `env` ([stage 0](stage-0-intake.md), step 5).

A part issued per account or per installation that is not secret (a host name, a database name, a port, a folder, a plant's base address) is a variable too, declared with `secret: false` and a description that says why it is safe to show: `env.<NAME>.secret: false`. The reason: in what `dta` relays from the twin's containers (the results of probe, profile, build, sync and acceptance), the value of every variable not declared `secret: false` is replaced by `***` wherever it appears, once the value is 6 characters or longer. A database named `registry` left secret blanks the word "registry" in all of those, a source id and a description included.

| Kind | Form | Example |
|---|---|---|
| `file`, folder on this machine | `file://${NAME}`, where the variable holds the folder's absolute path; or `file:///absolute/path` | `file://${GREENHOUSE_DATA_DIR}` |
| `file`, folder listed by a web server | `https://host/path/` | `https://data.example.org/exports/` |
| `sql`, PostgreSQL | `postgresql://${USER}:${PASSWORD}@${HOST}:5432/<database>` | `postgresql://${COLDSTORE_DB_USER}:${COLDSTORE_DB_PASSWORD}@${COLDSTORE_DB_HOST}:5432/assets` |
| `sql`, MySQL or MariaDB | `mysql+pymysql://${USER}:${PASSWORD}@${HOST}:3306/<database>` | |
| `http` | the base address, or `${NAME}` holding it | `${COLDSTORE_API_URL}` |

The address is used from inside a container, so:

- `localhost` and `127.0.0.1` are the container itself, never this machine. A local folder is mounted into the container at the same path; that is why a file location must be an absolute path or start with a variable.
- A database or interface running in another container on this machine: ask the user for the name of that container's network (`docker network ls` lists them), write it in `runtime.networks`, and use the container's name as the host.
- A database or interface running directly on this machine, outside containers, is reachable only at an address of this machine that containers can reach (its network address, not `127.0.0.1`). Ask the user for such an address; if there is none, tell the user the source cannot be reached from the twin as it stands.
- A SQLite file cannot be reached; ask the user to export its tables to CSV files and read those with a `file` source.

## Options

`sources.<source>.options` holds the connector's settings. Any key not listed below is refused by `dta source probe` with `source.invalid_config`, naming the option's path.

### file

```yaml
options:
  datasets:
    <dataset>:
      glob: moisture/*.csv     # relative to the location; files are read in name order
      format: csv              # csv, parquet or jsonl
      types:                   # optional: column types that guessing would get wrong
        bed_id: text
      timezone: UTC            # optional: zone of timestamps written without one
```

Every file of a dataset must have the same columns. Column type names: `text`, `integer`, `float`, `boolean`, `timestamp`, `date32`, or any Arrow type name.

Times in text files:

- The type `timestamp` is `timestamp[us, tz=UTC]`: it reads only times written with an offset (`2026-03-02T01:00:00+01:00`, `...Z`), and `timezone` does not apply to it. A time written without one (`2026-03-02 01:00`) fails the probe with `source.read_failed` ("expected a zone offset").
- For times written without an offset, leave the column out of `types` (it is recognised as a time) or give it `timestamp[us]`, and set `timezone` to the zone they were written in, such as `Europe/Stockholm`. They are read in that zone and stored in UTC.

### sql

```yaml
options:
  schema: public               # optional
  timezone: UTC                # optional: zone of timestamps stored without one
  connect_timeout_s: 10        # optional
  datasets:                    # optional: without it, every table of the schema is a dataset
    rooms:
      table: cold_rooms
    compressors:
      query: SELECT compressor_id, room_id, model FROM compressors WHERE retired = false
```

A query is one SELECT (or WITH) statement that only reads; anything else is refused.

### http

```yaml
options:
  headers:                     # every value may hold ${NAME} placeholders
    Authorization: Bearer ${COLDSTORE_API_TOKEN}
  timeout_s: 30                # optional
  max_attempts: 5              # optional
  max_wait_s: 60               # optional
  clock:                       # optional: where the source tells its current time
    path: /v2/time
    field: $.now
  datasets:
    readings:
      path: /v2/readings       # below the location; no query string
      params:                  # optional query parameters
        site: all
      records: $.items         # JSON path of the list of records in each answer
      paging:                  # optional; type none, cursor, page, offset or link
        type: cursor
        cursor_path: $.next
        cursor_param: cursor
      since:                   # optional: ask only for rows newer than the last read
        param: after
        format: iso            # iso, epoch_s or epoch_ms
      columns:                 # optional: the columns kept, with their types
        sensor_id: text
        ts: timestamp
        value: float
```

`columns`, when given, is the whole list of columns the dataset keeps: a field of the records it does not name is not read at all. Name every field a mapping, view, filter or cursor uses, or leave `columns` out and every field is kept with a guessed type. `dta source probe` lists, per dataset, the fields the declaration left out in `dropped_columns`; read it after every probe.

Paging keys per type: `cursor`: `cursor_path`, `cursor_param`; `page`: `page_param`, `first_page`, `total_path`; `offset`: `limit` (required), `offset_param`, `limit_param`, `total_path`; `link`: `next_url_path`. A variable used in `headers` or `params` must also be listed in `sources.<source>.env`, or the twin's containers do not receive it.

## Mappings

One mapping per dataset and class, under `sources.<source>.mappings.<mapping>`:

| Field | Value |
|---|---|
| `dataset` | A dataset of the source (as named in its options, or a table name), or a view. |
| `object_type` | The ontology class the rows become (never an abstract class). |
| `identity` | The columns whose values identify one object, from the profile's `candidate_keys` or `entity_columns`. The object's uid is the class's name in snake_case (for a subclass, its topmost parent's), a colon, and the identity values joined by a vertical bar: `bed:b07`, `cold_room:r1`. |
| `properties.<property>.column` | The column a property is read from. |
| `properties.<property>.expression` | Instead of `column`: one SQL expression over the dataset's columns, such as `level_cm / 100.0`. |
| `properties.<property>.unit` | The unit of the column in the source, as a UCUM code; values are converted to the property's unit. Give it for every property that has a `unit` in the ontology, and for no other (`ontology.unit` warns otherwise). |
| `valid_time` | The column holding when each row's values were true. Give it in every mapping that fills a property with `dta_role: state`, and in no mapping that fills a fixed property (`ontology.role` warns otherwise): map the fixed and the timed properties of one class in separate mappings. |
| `kind` | `observed` (the default: the source reported it), `inferred`, `assumed` (written down by a person, not measured) or `simulated`. |
| `mode` | `upsert` (the default: an object missing from a later read is kept) or `snapshot` (it is deleted). `snapshot` only for a register whose removed rows mean removed objects, and only when its mapping is the one mapping of that class in the whole manifest and has no `valid_time`; otherwise `upsert`. |
| `filter` | A SQL condition a row must meet, such as `status <> 'retired'`. |
| `cursor` | A column that only grows, such as the time column, so a later read can take only new rows. |

Rules:

- A reference property's column holds the identity value of the object referred to, the same value that object's own mapping reads as identity (`house_id` in the beds file holds the `house_id` of the houses file).
- Every required property of a class must be filled by some mapping of that class; otherwise every row is refused (`ontology.required_unmapped`).
- Several mappings may fill one class: the register gives a room its name, the telemetry its temperature. A row for an object no mapping has created yet still needs every required property.
- A link class is mapped like any class: its identity columns and one column for each of its two references.
- Map every property a question targets: `coverage.unmapped_target` warns otherwise, and the question cannot be answered.
- `sources.<source>.incremental.<dataset>` names the time column of a dataset that is read on from where the last run stopped (live twins); the dataset must appear in the profile.
- A property mapped without `valid_time` (a register's name, size or demand) is in force from the first version of the loaded history, before any timed value. A change to it that a later sync finds is written at that sync's version: the twin knows that it changed, not when.

`dta manifest validate` refuses, with `sync.mapping_invalid`, what the build's `load` step would refuse: a view that names a dataset without its source, a view of more than one statement, `snapshot` on a class other mappings also fill, `snapshot` with a `valid_time`, a property with both or neither of `column` and `expression`, `kind: simulated`, and two time-series mappings of one dataset with different `valid_time` columns.

## Views

`sources.<source>.views.<view>` holds one SELECT statement that joins or reshapes datasets before they are mapped; a mapping of the same source reads it as `dataset: <view>`. Write it this way:

- Name every dataset as `"<source>"."<dataset>"`, both parts in double quotes: `FROM "telemetry"."readings"`. A bare `FROM readings` is refused. A view may read datasets of any source of the manifest, and other views as `"<source>"."<view>"`.
- The SQL is DuckDB's, and the session runs in UTC.
- Names a `WITH` clause defines are used bare. One statement only, no `;` between statements.
- Mapping `filter` and `expression` are DuckDB SQL too.
- A dataset a view reads is read whole at every sync, unless `sources.<source>.incremental.<dataset>` names its time column: declare it for a growing readings dataset of a live twin (`incremental: {readings: ts}`). Every mapping that then reads it, directly or through a view, needs a `valid_time`.

### Readings in long format

When a source keeps one row per reading (`sensor_id`, `ts`, `value`) and a catalogue says what each sensor measures and where (`sensor_id`, `asset_id`, `quantity`), write one view per quantity the ontology has as a property, and map each view to the class whose objects carry the sensors:

```yaml
views:
  tank_levels: |
    SELECT s.asset_id, r.ts, r.value AS level_m
    FROM "telemetry"."readings" AS r
    JOIN "registry"."sensors" AS s ON s.sensor_id = r.sensor_id
    WHERE s.quantity = 'level'
mappings:
  tank_level:
    dataset: tank_levels
    object_type: Tank
    identity: [asset_id]
    valid_time: ts
    properties:
      level:
        column: level_m
        unit: m
```

The view `tank_levels` and the mapping belong to the same source (here `telemetry`). Repeat it for each quantity (`pressure` on junctions, `status` on pumps). A reading's quality flag is a `WHERE` condition of the view (`AND r.quality <> 'suspect'`); a value the control interface writes as text and the telemetry as a number is turned into the same form in the view (`CASE r.value WHEN 1 THEN 'on' ELSE 'off' END AS status`), so that what an action sets and what the twin observes compare equal ([live twins](live.md), "Whether an action had its effect").

The profile's `noise_sigma` of such a `value` column is one figure over every sensor and quantity together, and a property read through a view has no profile of its own: `feasible.noise` compares its tolerance with nothing. Propose such a tolerance from what the user's decisions need, and say in the handover that it was not compared with the noise of the data.

## Source settings the tool measures

`sources.<source>.profile` and, for timestamped data, `sources.<source>.refresh_period` are written by `dta profile`. `sources.<source>.rate_limit_period` is the shortest gap the source allows between two reads; write it only when the source's owner states one. `sources.<source>.edits_past` is `true` for a source that rewrites its own past rows; `sources.<source>.host_local` is `true` for a file on this machine that changes while the twin runs.
