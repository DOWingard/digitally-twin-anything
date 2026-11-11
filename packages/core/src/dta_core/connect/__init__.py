"""Read-only access to a twin's data sources: SQL databases, folders of files, and
JSON-over-HTTP interfaces.

`open_connector` turns a manifest's source entry into a `Connector` whose
`read` yields Arrow tables with one set of type rules (`normalize`); `probe`
checks every dataset quickly and `profile_source` measures what the datasets
hold and how fast they change.
"""

from __future__ import annotations

import os

# dlt sends usage telemetry to its vendor unless this is set before its runtime
# starts; it is set here, ahead of any import of dlt by this package.
os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"

from .factory import open_connector  # noqa: E402
from .normalize import SchemaMismatch, conform, normalize_table  # noqa: E402
from .probe import DatasetProbe, ProbeColumn, ProbeReport, probe  # noqa: E402
from .profile import profile_dataset, profile_source  # noqa: E402

__all__ = [
    "DatasetProbe",
    "ProbeColumn",
    "ProbeReport",
    "SchemaMismatch",
    "conform",
    "normalize_table",
    "open_connector",
    "probe",
    "profile_dataset",
    "profile_source",
]
