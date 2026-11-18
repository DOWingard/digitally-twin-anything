# Add-ons: a Python client, MCP tools and read-only SQL

Besides its HTTP API and operator skill, a twin can offer three more ways in. Each is chosen by the user in stage 1 ([question bank](question-bank.md)), is built, checked and described in the operator skill only while its field is `true`, and leaves nothing behind once it is `false` again: the next `dta build` removes it.

| Field | What the build makes | Variables to declare under `env` | Build step | Acceptance check |
|---|---|---|---|---|
| `contract.addons.sdk_python` | A Python package `twin_<id>` (the id with each run of other characters than letters and digits turned into `_`) in `<workspace>/sdk/python/`, with its own pyproject.toml and README.md. One typed accessor per object and link type, one method per question; tables come back as Arrow. | none: it reads the API's address and token from `DTA_BASE_URL` and `DTA_API_TOKEN` when it runs | `sdk`, after `skill` | `sdk.usable` |
| `contract.addons.mcp` | A service `mcp` that offers each API operation as an MCP tool, over streamable HTTP at `http://127.0.0.1:<DTA_MCP_PORT>/mcp`. It holds no token: the agent sends its own. | `DTA_MCP_PORT` (`secret: false`) | `mcp`, after `ui` | `mcp.reachable` |
| `contract.addons.readonly_sql` | The store's port published at `127.0.0.1:<DTA_SQL_PORT>`, and a store user `twin_reader` that may only read the twin's database, its history included. | `DTA_SQL_PORT` (`secret: false`) and `DTA_SQL_READER_PASSWORD` (secret) | `store` | `sql.readonly` |

## Declaring the variables

`dta manifest validate` reports `addons.env_undeclared` for each variable a selected add-on needs that `env` does not declare. Declare them as the table says, with a description, for example:

```yaml
env:
  DTA_MCP_PORT: {description: Host port of the twin's MCP adapter, secret: false}
  DTA_SQL_PORT: {description: Host port of read-only SQL over the twin's store, secret: false}
  DTA_SQL_READER_PASSWORD: {description: Password of the store user twin_reader}
```

Then `dta env template` adds them to `.env`: it generates `DTA_SQL_READER_PASSWORD` and leaves the two ports for the user to fill, like `DTA_API_PORT`. Ask the user to choose ports that are free; never ask for or show the password.

## Changing the choice after the build

Changing an add-on changes the contract: the user signs again (stage 5), then `dta build`. Switched off, the `mcp` step removes the adapter's container, the `store` step drops the reader and unpublishes the port, and the `sdk` step deletes `sdk/python/`; each says so (`detail.reason` of the steps `mcp` and `sdk`, `detail.reader` of `store`). The client package is generated again only when the ontology, its types or the API's description changed; `dta generate sdk --force` generates it at once.

## What fails

The codes of the add-ons are in [troubleshooting](troubleshooting.md), "Add-ons"; the failed steps `store`, `mcp` and `sdk` in its table of build steps; the failed checks `sdk.usable`, `mcp.reachable` and `sql.readonly` in [stage 7](stage-7-acceptance.md).

The operator skill describes each selected add-on in its section "Other ways in": the client's install and use, the MCP address and how to register it, and the SQL address, user and example queries.
