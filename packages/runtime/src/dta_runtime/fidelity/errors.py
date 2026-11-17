"""Why a fidelity call was refused, as stable codes."""

from __future__ import annotations

from typing import Any

from dta_core.errors import DtaError

# The twin has no recurring sync or no fidelity section: there is nothing to check.
NOT_LIVE = "fidelity.not_live"
# The twin's ontology predates the fidelity records: install it again to bring them in.
UNSUPPORTED = "fidelity.unsupported"
# A check at a time before the latest check's.
OUT_OF_ORDER = "fidelity.out_of_order"
NOT_FOUND = "fidelity.not_found"
BAD_REQUEST = "fidelity.bad_request"
# The record is not in a status the call applies to.
CONFLICT = "fidelity.conflict"


class FidelityError(DtaError):
    """A fidelity call was refused; `detail` names what it was about."""

    code = "fidelity.error"


def refused(code: str, message: str, **detail: Any) -> FidelityError:
    return FidelityError(message, code=code, detail=detail)
