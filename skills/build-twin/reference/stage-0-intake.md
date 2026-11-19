# Stage 0: intake

Goal: a workspace whose manifest names the twin, says what it is for, and names every source with a connector kind and a location.

Gate `intake` is computed from the manifest. It passes when `twin.id` and `contract.purpose` have values and at least one source is declared, each with `connector` and `location`.

## 1. Find out what the user has already said

Read the conversation. Note, for each item below, whether the user already gave it:

| Item | Field it fills |
|---|---|
| The system: what it is, in their words | `twin.name` |
| What the twin is for | `contract.purpose` |
| Where the system's data is: databases, HTTP APIs, files, a repository | `sources.<source>.connector`, `sources.<source>.location`, `sources.<source>.description` |

Ask for each missing item with the questions of the [question bank](question-bank.md) ("Intake questions"), one at a time, each with a proposed default.

What the user points at decides where the data comes from:

- A database, an HTTP API or a folder of files: that is a source. Write it as in step 5. An interface the twin would only send commands to is not a source, and a feed no question needs is left out ([sources](sources.md), "Which services are sources").
- A code repository: look in it (read only, never run its code) for data files, database schemas, connection settings and API descriptions. Propose a source for each data store you find, by kind and address, and ask the user to confirm each one. Never take a password or token from the repository: name the variable instead (step 5).
- Only a description, with no data anywhere: a twin is filled from data, so ask the user for files that list the system's objects (one CSV file per kind of object, one row per object). You may draft those files with the user from their description, in a folder the user names outside the workspace; map them with `kind: assumed` (see [sources](sources.md)) so the twin says they are assumptions.

## 2. Choose the twin's id, name and workspace

You decide these; do not ask, but tell the user what you chose.

- Id: from the system's name, in lowercase, matching `^[a-z][a-z0-9_-]{0,62}$` (a letter, then letters, digits, `-` or `_`). Example: `greenhouse`. It names the twin's containers (`dta-<id>`) and its operator skill.
- Name: the system's name as the user says it. Example: `Greenhouse soil moisture`.
- Workspace: the directory the user named; otherwise a new directory `twin-<id>` in the current directory. Use its absolute path as `<workspace>` from now on.

## 3. Create the workspace

```sh
dta status
```

- Exit 2, code `workspace.missing`: create it:

  ```sh
  dta init --id <id> --name "<name>"
  ```

  Exit 0 with `result.created` true: the workspace exists, with `manifest.yaml`, `ontology/`, `profiles/`, `tasks/`, `stack/`, `.gitignore` (listing `.env`) and `.dta/`. Exit 2 with code `init.bad_id`: the id does not match the pattern; choose another and run it again.
- Exit 0: a workspace is already there. Do not run `dta init`; read `result.stage` and continue at that stage (see "Start or resume" in the skill).

## 4. Start the record of the discussion

Create `<workspace>/discussion.md` with the line `# Discussion of the <name> twin`, then append one entry for every question you asked so far, in the format given in the skill ("The record of the discussion").

## 5. Write what you know into the manifest

Open `<workspace>/manifest.yaml` with your file tools. `dta init` wrote `twin`, `interfaces`, `env` (the twin's own four variables) and `runtime`; keep them. Add:

1. `contract.purpose`: one or two sentences, in the user's words, saying what the twin is for and for whom.
2. For each source, one entry under `sources`, keyed by a short id you choose (`^[a-z][a-z0-9_-]{0,62}$`, such as `logger` or `register`):
   - `sources.<source>.description`: what the source is, in the user's words.
   - `sources.<source>.connector`: `file`, `sql` or `http`. How to choose: [sources](sources.md), "Connector kinds".
   - `sources.<source>.location`: its address, with every secret and every machine-specific part replaced by a `${NAME}` placeholder. Forms per kind: [sources](sources.md), "Locations".
   - `sources.<source>.env`: the list of every variable the source's location and options name.
3. For every variable you named, an entry under `env`:
   - `env.<NAME>.description`: what the value is, so the user knows what to write. Example: `Password of the asset register database`.
   - `env.<NAME>.secret`: `false` for every value that is safe to show (a folder path, a host name, a database name, a port number, an address issued per account), with the reason in its description; `true` (the default) for passwords, tokens and keys. A value left secret is masked as `***` wherever it appears in what `dta` relays, so a database named `registry` would blank that word everywhere ([sources](sources.md), "Locations").

Never write a password, token, key or personal address into `manifest.yaml`: the check `secret.inline` refuses a location or header that carries one, and the manifest is meant to be shared.

Leave `options` and `mappings` of each source for later stages: the options are written in stage 2 when needed to reach the source, and the mappings in stage 4.

## 6. Check the gate

```sh
dta status
```

Read the `result` of the gate whose `name` is `intake` in `result.gates`:

- `passed`: go to [stage 1](stage-1-contract.md).
- `failed`: each entry of `reasons` names a field without a value (such as `sources.logger.location has no value`). Fill it, from what you know or by asking the user, and run `dta status` again.
- An error `manifest.schema` (exit 2): the YAML you wrote does not match the schema; `error.detail.problems` lists each path and problem. Fix those and run it again.
