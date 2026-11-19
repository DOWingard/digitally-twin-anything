# Examples

Two complete workspaces, as they stand at the end of stage 4: a manifest and an ontology each, written by hand, with everything `dta` writes itself left out (profiles, refresh periods, the derived sync interval, `field_status`). Both pass `dta manifest validate` with no finding. Compare yours with them for shape; never copy their names, values or addresses into a twin of another system.

## A static twin over files

[static-greenhouse](examples/static-greenhouse/manifest.yaml), with its ontology [greenhouse.yaml](examples/static-greenhouse/ontology/greenhouse.yaml).

A data logger's CSV exports of one growing season: houses, beds and hourly soil moisture readings. It shows:

- a `file` source whose folder is named by a variable, with three datasets, one of them a pattern over many files;
- two mappings onto one class (`Bed`): one for its fixed properties, one for the timed readings with a `valid_time`;
- a reference property (`Bed.house`) that links the objects;
- a descriptive question answered by `latest` with its property given in the example, and a diagnostic one answered by `below` with its parameters fixed;
- `sync.backfill_interval`, so the recorded season loads as hourly versions.

## A live twin over an HTTP interface and a database

[live-coldstore](examples/live-coldstore/manifest.yaml), with its ontology [coldstore.yaml](examples/live-coldstore/ontology/coldstore.yaml) and its code [coldstore.py](examples/live-coldstore/code/coldstore.py).

A refrigerated warehouse: the asset register in PostgreSQL, readings from a telemetry interface every five minutes, and a control interface that sets each room's setpoint. It shows:

- a `sql` source with one table and one query, and an `http` source with a bearer token in a header, cursor paging and a `since` parameter;
- secrets only as `${NAME}` placeholders, each declared under `env`;
- a predictive question with its horizon, its model (three shipped families) and its `forecast` function, all named after the question;
- a supervised control carried out by an action with an HTTP executor, `contract.addons.executors: true`, an objective computed by `aggregate`, and a constraint;
- a control loop whose policy is a registered function in the code directory;
- webhook notifications and the fidelity settings of a live twin; `sync.interval` is left for `dta sync derive --write`.
