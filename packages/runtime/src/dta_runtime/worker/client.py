"""The twin and its artifacts as the code a run executes sees them: through the API.

Every call carries the token, the run's trace id (so the API logs it under the
run's trace) and a timeout no longer than what is left of the run's budget. An
error the API answers with a problem document becomes a `DtaError` with the API's
code and message.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from typing import Any
from urllib.parse import quote

import httpx
import pyarrow as pa
from dta_core.errors import DtaError
from dta_core.log import Log

from ..protocol import FrameRef, FunctionRequest, PredictRequest
from .errors import API_UNREACHABLE, ARROW_MEDIA, BAD_STATE, TIMEOUT

log = Log("worker.client")

API_PREFIX = "/api/v1"
NEXT_CURSOR_HEADER = "x-dta-next-cursor"
TRACEPARENT = "traceparent"
VERSIONS = f"{API_PREFIX}/history/versions"
TRAINING_SET = f"{API_PREFIX}/block/training-set"
ARTIFACTS = f"{API_PREFIX}/artifacts"
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
# Pages of one listing: far more than any listing has, so a cursor that never ends
# cannot keep a run reading until its budget is gone without saying why.
MAX_PAGES = 100_000


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


class Calls:
    """Requests to the API on behalf of one run."""

    def __init__(self, http: httpx.Client, *, deadline: float, trace_id: str) -> None:
        self._http = http
        self._deadline = deadline
        self._headers: dict[str, str] = {}
        if _TRACE_ID.match(trace_id) and set(trace_id) != {"0"}:
            self._headers[TRACEPARENT] = f"00-{trace_id}-{secrets.token_hex(8)}-01"
        else:
            log.debug("the run's trace id is not a W3C trace id; the API makes its own")

    def remaining(self) -> float:
        return self._deadline - time.monotonic()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **options: Any,
    ) -> httpx.Response:
        left = self.remaining()
        if left <= 0:
            log.warn("the run's budget is spent; the API is not called", method=method, path=path)
            raise DtaError("the run's budget is spent", code=TIMEOUT)
        started = time.perf_counter()
        try:
            response = self._http.request(
                method,
                path,
                params=params,
                headers={**self._headers, **(headers or {})},
                timeout=httpx.Timeout(left),
                **options,
            )
        except httpx.TimeoutException as exc:
            log.warn(
                "a call to the API outlasted the run's budget",
                method=method,
                path=path,
                timeout_s=round(left, 3),
                error=type(exc).__name__,
            )
            raise DtaError("the run's budget ran out waiting for the API", code=TIMEOUT) from exc
        except httpx.HTTPError as exc:
            log.error(
                "the API cannot be reached",
                exc=exc,
                method=method,
                path=path,
                elapsed_ms=_elapsed_ms(started),
            )
            raise DtaError(
                f"the API cannot be reached: {type(exc).__name__}", code=API_UNREACHABLE
            ) from exc
        log.debug(
            "API called",
            method=method,
            path=path,
            params=sorted(params or {}),
            status=response.status_code,
            elapsed_ms=_elapsed_ms(started),
        )
        if response.status_code >= 400:
            raise _problem(response, method, path)
        return response


def _problem(response: httpx.Response, method: str, path: str) -> DtaError:
    document: Any = None
    try:
        document = response.json()
    except ValueError:
        pass
    if isinstance(document, dict) and isinstance(document.get("code"), str):
        code, message = document["code"], str(document.get("detail") or document["code"])
        log.warn(
            "the API refused a call",
            method=method,
            path=path,
            status=response.status_code,
            code=code,
        )
        return DtaError(message, code=code, detail={"status": response.status_code})
    log.error(
        "the API answered an error without a problem document",
        method=method,
        path=path,
        status=response.status_code,
    )
    return DtaError(
        f"the API answered {response.status_code} without a problem document",
        code=API_UNREACHABLE,
        detail={"status": response.status_code},
    )


def _arrow(response: httpx.Response, method: str, path: str) -> pa.Table:
    media = response.headers.get("content-type", "").split(";")[0].strip()
    if media != ARROW_MEDIA:
        log.error("the API did not answer a table", method=method, path=path, media_type=media)
        raise DtaError(
            f"the API answered {media or 'nothing'} where a table was asked for",
            code=API_UNREACHABLE,
        )
    return pa.ipc.open_stream(response.content).read_all()


class ApiTwin:
    """A `Reader` of the twin through the API, at one version of one branch."""

    def __init__(self, calls: Calls, request: Any) -> None:
        self._calls = calls
        if isinstance(request, FunctionRequest):
            self.at, self.branch = request.at, request.branch
        else:
            # A model reads the twin where its frame ends: the last version it may know
            # of, on the branch it is asked about. A family that builds itself from the
            # graph (a physical model) then sees a scenario's assumed changes, and
            # nothing later than what it predicts from.
            frame = request.history if isinstance(request, PredictRequest) else request.frame
            # Resolved once, so that every read of the run names the same version by id.
            found = calls.request(
                "GET",
                f"{VERSIONS}/{quote(frame.end, safe='')}",
                params={"branch": frame.branch},
            ).json()["data"]
            self.at, self.branch = str(found["id"]), str(found["branch"])
        log.debug("reader of the twin", at=self.at, branch=self.branch)

    def _params(self, params: dict[str, Any]) -> dict[str, Any]:
        found = {name: value for name, value in params.items() if value is not None}
        found.setdefault("at", self.at)
        found.setdefault("branch", self.branch)
        return found

    def get(self, path: str, **params: Any) -> Any:
        return self._calls.request("GET", API_PREFIX + path, params=self._params(params)).json()

    def table(self, path: str, **params: Any) -> pa.Table:
        query = {**self._params(params), "format": "arrow"}
        pages: list[pa.Table] = []
        cursor: str | None = None
        while len(pages) < MAX_PAGES:
            asked = query if cursor is None else {**query, "cursor": cursor}
            response = self._calls.request(
                "GET", API_PREFIX + path, params=asked, headers={"Accept": ARROW_MEDIA}
            )
            pages.append(_arrow(response, "GET", path))
            cursor = response.headers.get(NEXT_CURSOR_HEADER)
            if not cursor:
                break
        else:
            log.error("a listing did not end", path=path, pages=len(pages))
            raise DtaError(
                f"the listing of {path} did not end after {MAX_PAGES} pages", code=API_UNREACHABLE
            )
        log.debug("table read", path=path, pages=len(pages))
        return pages[0] if len(pages) == 1 else pa.concat_tables(pages)

    def frame(self, ref: FrameRef) -> pa.Table:
        response = self._calls.request(
            "POST",
            TRAINING_SET,
            params={"format": "arrow"},
            headers={"Accept": ARROW_MEDIA},
            json=ref.model_dump(mode="json"),
        )
        return _arrow(response, "POST", TRAINING_SET)


class ApiArtifacts:
    """The twin's `Artifacts` through the API; what is read is checked against its name."""

    def __init__(self, calls: Calls) -> None:
        self._calls = calls

    def get(self, sha256: str) -> bytes:
        content = self._calls.request("GET", f"{ARTIFACTS}/{sha256}").content
        found = hashlib.sha256(content).hexdigest()
        if found != sha256:
            log.error(
                "an artifact's content does not hash to its name",
                sha256=sha256,
                found=found,
                size_bytes=len(content),
            )
            raise DtaError(
                f"artifact {sha256} holds content that hashes to {found}", code=BAD_STATE
            )
        log.debug("artifact read", sha256=sha256, size_bytes=len(content))
        return content

    def put(self, content: bytes, media_type: str) -> str:
        expected = hashlib.sha256(content).hexdigest()
        response = self._calls.request(
            "POST", ARTIFACTS, content=content, headers={"Content-Type": media_type}
        )
        stored = str(response.json().get("sha256"))
        if stored != expected:
            log.error("the API kept an artifact under another hash", sha256=expected, kept=stored)
            raise DtaError(f"the API kept the artifact as {stored}, not {expected}", code=BAD_STATE)
        log.info("artifact stored", sha256=stored, size_bytes=len(content), media_type=media_type)
        return stored
