"""Errors raised by the store, each with a stable code under `store.`."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..errors import DtaError
from ..schema import Violation


class StoreError(DtaError):
    """Base class of every error the store raises."""

    code = "store.error"


class StoreUnavailable(StoreError):
    """The database cannot be reached, or no pooled connection became free in time."""

    code = "store.unavailable"


class StoreClosed(StoreError):
    code = "store.closed"


class NotInstalled(StoreError):
    """The database does not exist or holds no installed twin."""

    code = "store.not_installed"


class InvalidOntology(StoreError):
    """The ontology cannot be turned into tables safely."""

    code = "store.invalid_ontology"


class OntologyMismatch(StoreError):
    """The ontology given to the store is not the one in force where it is being used."""

    code = "store.ontology_mismatch"


class InvalidInput(StoreError):
    """An argument is malformed. The specific code is passed by the raiser:
    `store.invalid_meta`, `store.invalid_provenance`, `store.naive_datetime`,
    `store.bad_time`, `store.bad_selector`, `store.bad_filter`,
    `store.bad_branch_name`, `store.invalid_settings`."""

    code = "store.invalid_input"


class ViolationError(StoreError):
    """A change set was refused; `violations` lists every reason found."""

    code = "store.violation"

    def __init__(self, violations: list[Violation], *, branch: str) -> None:
        first = violations[0]
        where = f"{first.type}/{first.uid}" if first.uid else (first.type or "change set")
        suffix = "" if len(violations) == 1 else f" (and {len(violations) - 1} more)"
        super().__init__(
            f"change set refused on {branch}: {where}: {first.message}{suffix}",
            detail={"branch": branch, "violations": [asdict(v) for v in violations]},
        )
        self.violations = list(violations)


class TimeRegression(StoreError):
    """The version time given is earlier than the time of the branch head."""

    code = "store.time_regression"


class HistoryRewritten(StoreError):
    """A version the store knew is no longer at the same position on its branch."""

    code = "store.history_rewritten"


class ForeignCommit(StoreError):
    """The branch holds a commit that was not written by this layer."""

    code = "store.foreign_commit"


class HeadMoved(StoreError):
    """A commit landed on a head other than the one read at the start of the commit."""

    code = "store.head_moved"


class LockTimeout(StoreError):
    """Another committer held the database lock for longer than the configured wait."""

    code = "store.lock_timeout"


class RecoveryFailed(StoreError):
    """Uncommitted changes found in the working set could not be discarded."""

    code = "store.recovery_failed"


class UnknownVersion(StoreError):
    code = "store.unknown_version"


class UnknownBranch(StoreError):
    code = "store.unknown_branch"


class BranchExists(StoreError):
    code = "store.branch_exists"


class ProtectedBranch(StoreError):
    """The live branch cannot be deleted."""

    code = "store.protected_branch"


class UnknownType(StoreError):
    code = "store.unknown_type"


class UnknownProperty(StoreError):
    code = "store.unknown_property"


class MergeConflict(StoreError):
    """The two branches cannot be merged without choosing between changes."""

    code = "store.merge_conflict"


class NothingToMerge(StoreError):
    code = "store.nothing_to_merge"


class MigrationRequired(StoreError):
    """The new ontology changes the old one in a way that is not purely additive."""

    code = "store.migration_required"

    def __init__(self, changes: list[str]) -> None:
        suffix = "" if len(changes) == 1 else f" (and {len(changes) - 1} more)"
        super().__init__(
            f"ontology change is not additive: {changes[0]}{suffix}",
            detail={"changes": list(changes)},
        )
        self.changes = list(changes)


def invalid(code: str, message: str, **detail: Any) -> InvalidInput:
    return InvalidInput(message, code=f"store.{code}", detail=detail)
