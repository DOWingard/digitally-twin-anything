# Sources and mappings

A source is one place the twin reads data from: a folder of files, a database or an HTTP interface. Every source is read only, from inside the twin's containers, with the connector its `connector` field names. A mapping says how the rows of one dataset of a source become objects of one ontology class.

## Connector kinds

| The data is in | `sources.<source>.connector` | Reads |
|---|---|---|
| CSV, Parquet or JSON Lines files in a folder on this machine, or in a folder a web server lists | `file` | Each dataset is a file name pattern inside the folder. |
| A PostgreSQL, MySQL or MariaDB database | `sql` | Each dataset is a table or one SELECT statement. |
| An HTTP interface that answers JSON | `http` | Each dataset is one endpoint, with its paging. |

The kind `custom` is accepted by the manifest but cannot be opened by the tool: do not use it. If a source fits none of the three kinds, tell the user which part cannot be read and ask whether the data can be exported to files.

## Locations

`sources.<source>.location` is the source's address. Put every secret part, and every part that differs between machines, in a `${NAME}` placeholder, and name each variable in `sources.<source>.env` and under `env` ([stage 0](stage-0-intake.md), step 5).

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
      columns:                 # optional: column types
        ts: timestamp
```

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
| `mode` | `upsert` (the default: an object missing from a later read is kept) or `snapshot` (it is deleted). Use `snapshot` for a register whose removed rows mean removed objects. |
| `filter` | A SQL condition a row must meet, such as `status <> 'retired'`. |
| `cursor` | A column that only grows, such as the time column, so a later read can take only new rows. |

Rules:

- A reference property's column holds the identity value of the object referred to, the same value that object's own mapping reads as identity (`house_id` in the beds file holds the `house_id` of the houses file).
- Every required property of a class must be filled by some mapping of that class; otherwise every row is refused (`ontology.required_unmapped`).
- Several mappings may fill one class: the register gives a room its name, the telemetry its temperature. A row for an object no mapping has created yet still needs every required property.
- A link class is mapped like any class: its identity columns and one column for each of its two references.
- Map every property a question targets: `coverage.unmapped_target` warns otherwise, and the question cannot be answered.
- `sources.<source>.views.<view>` may hold a named SELECT over the source's datasets (read in the same run), mapped as `dataset: <view>`, when one table must be joined or reshaped first.
- `sources.<source>.incremental.<dataset>` names the time column of a dataset that is read on from where the last run stopped (live twins); the dataset must appear in the profile.

## Source settings the tool measures

`sources.<source>.profile` and, for timestamped data, `sources.<source>.refresh_period` are written by `dta profile`. `sources.<source>.rate_limit_period` is the shortest gap the source allows between two reads; write it only when the source's owner states one. `sources.<source>.edits_past` is `true` for a source that rewrites its own past rows; `sources.<source>.host_local` is `true` for a file on this machine that changes while the twin runs.
