"""Whether the manifest names the version of the format it is written in.

A version newer than this software reads, or a field newer than the version named,
is refused when the manifest is read; what is left to say here is a manifest that
names none, which is read as the newest version.
"""

from __future__ import annotations

from collections.abc import Iterator

from ..models import MANIFEST_VERSION
from .findings import Issue, Severity, check
from .view import ManifestView

UNSET = "manifest.version_unset"


@check(UNSET, Severity.warning)
def version_unset(view: ManifestView) -> Iterator[Issue]:
    if view.manifest.manifest_version is None:
        yield Issue(
            "manifest_version",
            f"the manifest names no version; it is read as {MANIFEST_VERSION}, the newest this "
            f"software reads: write manifest_version: {MANIFEST_VERSION}",
        )
