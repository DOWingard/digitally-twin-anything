"""Error types shared by every package.

Each error carries a stable machine-readable code so callers (the command line
tool, the HTTP API, an agent reading output) can branch on the code instead of
parsing messages.
"""

from __future__ import annotations

from typing import Any


class DtaError(Exception):
    """Base class. `exit_code` is what the command line tool exits with."""

    code = "dta.error"
    exit_code = 2

    def __init__(
        self, message: str, *, code: str | None = None, detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class ConfigError(DtaError):
    """The process environment is incomplete or malformed."""

    code = "config.invalid"


class ManifestError(DtaError):
    """The manifest file cannot be read or does not match the schema."""

    code = "manifest.invalid"


class ManifestTooNew(ManifestError):
    """The manifest is written in a newer version of the format than this software reads.

    `detail` holds `manifest_version` (the manifest's) and `reads` (the newest this
    software reads), so a caller can say which side is older.
    """

    code = "manifest.too_new"


class WorkspaceError(DtaError):
    """The twin workspace is missing, damaged or in an unexpected state."""

    code = "workspace.invalid"


class LockTimeout(WorkspaceError):
    """Another process holds the workspace lock."""

    code = "workspace.locked"


class CheckFailed(DtaError):
    """A gate or check ran correctly and its answer is no."""

    code = "check.failed"
    exit_code = 1
