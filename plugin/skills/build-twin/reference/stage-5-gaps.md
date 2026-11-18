# Stage 5: gaps and signature

Goal: no required field left open, no error finding, and the user's signature on the contract as it now stands.

Gate `gaps` is computed. It passes when no required field is `unknown` or `proposed`, `dta manifest validate` finds no error, and the latest signature is for the current contract. Its reasons say which of these is missing: `N gap(s) remain`, `N error finding(s)`, `the contract is not signed`, `the contract changed since it was signed`.

## 1. List what is open

```sh
dta manifest validate --complete
```

- Exit 0: no error and no gap. Go to step 3.
- Exit 1, code `validate.failed`: `error.detail.findings` lists the findings and `error.detail.gaps` every open field with its `path`, `status` and `description`.

For each finding of severity `error`: fix it ([troubleshooting](troubleshooting.md) by its `code`).

Then see who owns each gap:

```sh
dta manifest gaps
```

`result.by_owner.agent` and `result.by_owner.user` list the open fields of each owner, with `path`, `status` and `description`.

For each gap owned by the agent: write the field yourself (its stage's reference says how). The gaps you meet here are usually `sources.<source>.profile` (run `dta profile`), `sources.<source>.refresh_period` (profile again, or ask the user how often the source delivers), and `sync.interval` (`dta sync derive --write`).

For each gap owned by the user with status `unknown`: ask for it with the [question bank](question-bank.md).

Run `dta manifest validate --complete` again until the only gaps left are user-owned fields with status `proposed`: values you wrote that the user has not confirmed (the tolerances and stalenesses, `ontology.type_signature` if it changed, any field changed since it was confirmed).

## 2. Show the open values, then confirm what the user agrees to

Show the user every remaining `proposed` field in plain words, with what was measured where it matters ("q_temp: mean absolute error at most 0.5 °C; the sensors scatter by 0.2 °C"). Ask: "Do you agree to these values?" Change what the user names. When the user agrees:

```sh
dta manifest confirm <path> <path>
```

with every path the user agreed to. Then validate again, which also records the design gate:

```sh
dta manifest validate --complete
```

Exit 0 is required before you go on.

## 3. Show the summary

```sh
dta manifest summary
```

`result.markdown` is the one-page summary of the contract and the design: the purpose, each question with its tolerance and staleness, the controls, the sources, the types. Show it to the user whole, as it is, and ask:

> This is the contract the twin will be built and checked against. Do you approve it? If you do, I will sign it on your behalf with your name; or you can sign it yourself by running `dta -w <workspace> manifest sign` in your terminal and typing the twin's id.

## 4. Sign

Only after the user's explicit yes to the summary you showed ("yes", "approved", "sign it"; not "looks fine so far", not silence):

- If the user signs in their own terminal: wait until they say they did, then run `dta status`.
- Otherwise ask for their name if you do not know it, and sign for them:

  ```sh
  dta manifest sign --non-interactive --approved-by "<user's name>"
  ```

  Record in `discussion.md`: the field `contract`, the question "Do you approve the summary?", the answer in the user's words, and "signed on the user's behalf".

Outcomes:

- Exit 0: the signature is recorded, bound to the contract and the summary shown.
- Exit 1, code `sign.gaps` or `sign.errors`: something is still open; go back to step 1.
- Exit 2, code `sign.changed`: the manifest changed while signing; show the summary again and ask again.
- Exit 2, code `sign.not_interactive`: you ran `dta manifest sign` without `--non-interactive`; your shell has no terminal for the user. Use the form above.

Never sign without the yes, never with your own name, and never sign again after a contract change without showing the new summary.

## 5. Check the gate

```sh
dta status
```

Gates 0 to 5 must all be `passed`. Gate `design` `stale`: run `dta manifest validate`. Gate `gaps` `failed` with "the contract changed since it was signed": the contract changed after signing; start this stage again from step 1. Then go to [stage 6](stage-6-build.md).
