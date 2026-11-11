"""The operations a store must support to hold a typed graph with full history."""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

import pyarrow as pa

from ..fixture import ChangeSet
from ..log import Log

PROJECT_DIR = Path(__file__).resolve().parents[2]

# Invalid writes every engine is asked to refuse.
REJECTION_CASES = ("bad_enum", "missing_required", "dangling_ref", "wrong_type", "below_minimum")


class Engine(ABC):
    """`at` is a version id returned by `commit`; None means the head of the live line."""

    name: str

    @abstractmethod
    def reset(self) -> None:
        """Drop any previous database and create an empty one."""

    @abstractmethod
    def install_schema(self) -> list[str]:
        """Install the ontology; return constraints the engine cannot enforce itself."""

    @abstractmethod
    def commit(self, changes: ChangeSet, message: str, branch: str | None = None) -> str:
        """Apply a change set as one version and return its id."""

    @abstractmethod
    def get_object(
        self, type_name: str, uid: str, at: str | None = None, branch: str | None = None
    ) -> dict | None: ...

    @abstractmethod
    def node_table(self, type_name: str, at: str | None = None) -> pa.Table:
        """All objects of a type at a version, as a columnar table."""

    @abstractmethod
    def property_history(self, type_name: str, uid: str, prop: str) -> list[tuple[str, object]]:
        """(version id, value) for every version in which the object was written."""

    @abstractmethod
    def diff_count(self, before: str, after: str, type_name: str) -> int:
        """Number of objects of a type that differ between two versions."""

    @abstractmethod
    def neighbors(self, uid: str, depth: int, at: str | None = None) -> set[str]:
        """Assets reachable along Feeds links in 1..depth hops, excluding the seed."""

    @abstractmethod
    def branch_create(self, name: str, at: str) -> None: ...

    @abstractmethod
    def branch_delete(self, name: str) -> None: ...

    @abstractmethod
    def rejections(self) -> dict[str, bool]:
        """For each case in REJECTION_CASES, whether the engine refused the write."""

    @abstractmethod
    def clone(self) -> "Engine":
        """An independent connection to the same database, for concurrent use."""

    @abstractmethod
    def close(self) -> None: ...

    # Path of the data directory inside the engine's container.
    _service: str
    _data_path: str

    def disk_bytes(self) -> int | None:
        """Size of the engine's data directory, or None when it cannot be measured."""
        log = Log(f"engine.{self.name}")
        command = ["docker", "compose", "exec", "-T", self._service, "du", "-sk", self._data_path]
        try:
            output = subprocess.run(
                command, cwd=PROJECT_DIR, capture_output=True, text=True, timeout=120, check=True
            ).stdout
            return int(output.split()[0]) * 1024
        except (subprocess.SubprocessError, ValueError, IndexError, OSError) as exc:
            log.warn("could not measure data directory size", error=repr(exc), command=command)
            return None
