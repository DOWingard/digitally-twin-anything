"""Delegated tasks with private write zones, leases and a serial merge.

Work is handed to other agents as tasks. A task declares the manifest paths it
reads and the manifest keys it owns; its only other write zone is its own
`tasks/<id>/output/`. Tasks run at the same time only when no task writes what
another reads or writes, and the toolkit, not the agent, works that out.

    plan --open   baseline the shared files, pin and prepare the next batch
    claim         take the lease on one task (one holder at a time)
    submit        check the finished task and seal its output
    merge         apply sealed results to the manifest, one at a time
    abort         give up a task or the batch

A task never changes shared state itself. It returns a patch for the keys it
owns, which the merge applies under the workspace lock, and proposals for
anything else, which are only stored.

Modules, from the rule outwards:

    paths       dotted paths, owned-key patterns, overlap by whole segments
    catalogue   the table of task kinds and the tasks a manifest calls for
    batches     which tasks may share a batch
    planning    a stage's batches and which one is next
    state       the record of batches, pins and leases (`.dta/tasks.json`)
    tree        reading task directories without following links
    zone        what may exist under `tasks/` and what must stay fixed
    result      the result file and how its patch is applied
    checks      every check a submit or merge runs, each with its own code
    durable     shared-file writes that survive the death of their process
    lifecycle   the commands, each one critical section under the lock
    cli         the `dta tasks` command line
"""
