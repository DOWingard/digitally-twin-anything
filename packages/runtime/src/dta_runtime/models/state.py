"""A fitted state as it is stored: a header anyone can read, then the pickle.

Bytes: one line of JSON (the header), a newline, the pickle of what the
family's `fit` returned. Loading a pickle runs code, so a state is loaded
only here, inside the worker, and only after its header says it is a state of
this format, of the family asked for, made by libraries this process can
read, and that the bytes after it are the ones that were written.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import platform
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from importlib import metadata
from typing import Any

from dta_core.log import Log

from ..protocol.worker import FamilyRef
from .errors import bad_state

log = Log("models.state")

MEDIA_TYPE = "application/vnd.dta.model-state"
FORMAT = 1
# What a state names the version of: a pickle made by another major version of one of
# these may not load, or may load into something that behaves differently.
LIBRARIES = ("statsforecast", "mlforecast", "scikit-learn", "numpy")
_REQUIRED = ("format", "family", "params", "step", "horizon", "columns", "versions", "body")


def versions() -> dict[str, str]:
    found = {"python": platform.python_version()}
    found.update({name: metadata.version(name) for name in LIBRARIES})
    return found


def family_document(family: FamilyRef) -> dict[str, str]:
    return family.model_dump(exclude_none=True)


def dump(
    state: Any,
    *,
    family: FamilyRef,
    params: Mapping[str, Any],
    step: timedelta,
    horizon: int,
    levels: Any,
    columns: Sequence[str],
    fitted_from: datetime,
    fitted_to: datetime,
    series: int,
    validity: Mapping[str, Sequence[float]],
) -> bytes:
    body = pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)
    header = {
        "format": FORMAT,
        "family": family_document(family),
        "params": dict(params),
        "step": step.total_seconds(),
        "horizon": horizon,
        # The coverages the family gives intervals for: "any", or a list (empty: none).
        "levels": levels,
        "columns": list(columns),
        "fitted_from": fitted_from.isoformat(),
        "fitted_to": fitted_to.isoformat(),
        "series": series,
        "validity": {name: [float(low), float(high)] for name, (low, high) in validity.items()},
        "versions": versions(),
        "body": {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()},
    }
    return json.dumps(header, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n" + body


def read_header(content: bytes) -> tuple[dict[str, Any], bytes]:
    """The header of a stored state and the bytes after it. Nothing is unpickled."""
    line, newline, body = content.partition(b"\n")
    if not newline:
        raise bad_state("not a fitted state: it has no header line")
    try:
        header = json.loads(line)
    except ValueError as exc:
        raise bad_state("not a fitted state: its first line is not JSON") from exc
    if not isinstance(header, dict) or header.get("format") != FORMAT:
        found = header.get("format") if isinstance(header, dict) else None
        raise bad_state(
            f"a fitted state of format {found!r}; this worker reads format {FORMAT}", format=found
        )
    missing = [name for name in _REQUIRED if name not in header]
    if missing:
        raise bad_state(f"the state's header lacks {', '.join(missing)}", missing=missing)
    return header, body


def _major(version: str) -> str:
    return version.split(".", 1)[0]


def check(header: Mapping[str, Any], body: bytes, family: FamilyRef) -> None:
    """Refuse a state that is not the one asked for, or that this process should not load."""
    if header["family"] != family_document(family):
        raise bad_state(
            f"the state was fitted by {header['family']}, the request names "
            f"{family_document(family)}",
            fitted_by=header["family"],
        )
    made_by, here = header["versions"], versions()
    for name in ("python", *LIBRARIES):
        if name in made_by and _major(str(made_by[name])) != _major(here[name]):
            raise bad_state(
                f"the state was made by {name} {made_by[name]}; this worker has {here[name]}, "
                "another major version: fit again",
                library=name,
                made_by=made_by[name],
                installed=here[name],
            )
    expected = header["body"]
    if len(body) != expected.get("bytes") or (
        hashlib.sha256(body).hexdigest() != expected.get("sha256")
    ):
        raise bad_state(
            f"the state is truncated or altered: {len(body)} bytes follow its header, "
            f"{expected.get('bytes')} were written",
            bytes=len(body),
        )


def load(content: bytes, family: FamilyRef) -> tuple[dict[str, Any], Any]:
    """The header and the fitted state, the header checked before anything is loaded."""
    header, body = read_header(content)
    check(header, body, family)
    try:
        return header, pickle.loads(body)
    except Exception as exc:
        log.error("a fitted state passed its checks and did not load", exc=exc)
        raise bad_state(f"the state did not load ({type(exc).__name__})") from exc
