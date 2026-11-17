"""Writing rows that were worked out from the twin back into it."""

from __future__ import annotations

import anyio.to_thread
from dta_core.log import Log
from fastapi import APIRouter, Request
from pydantic import ValidationError
from starlette.responses import Response

from .. import derived
from ..deps import ServicesDep
from ..docs import API_PREFIX, VERSION_HEADERS, errors
from ..formats import json_response
from ..models import DerivedRequest, DerivedWritten
from ..problems import ApiError
from ..tables import version_json

router = APIRouter(prefix=f"{API_PREFIX}/derived", tags=["derived"])
log = Log("api.derived")


@router.post(
    "",
    status_code=201,
    summary="Write derived rows as one version",
    description="Rows of one type, each `uid` and the properties written, as facts of `kind` "
    "(`inferred`, `assumed` or `simulated`) made by `source` (the uid of a function or a "
    "model) from the version `inputs_version`. One version of `branch` at the time of its "
    "head, cause `derived`; every value written carries the source, the kind, the "
    "confidence and when it was true (`t_valid`, else the time of the inputs), and the "
    "version records the source and the inputs. Observed facts are a sync run's to write; "
    "assumed and simulated ones belong on a scenario's branch; a property the manifest "
    f"fills from a source is never overwritten. At most {derived.MAX_ROWS} rows and "
    f"{derived.MAX_BYTES} bytes.",
    response_model=DerivedWritten,
    responses={
        201: {"headers": dict(VERSION_HEADERS)},
        **errors(
            400,
            403,
            404,
            409,
            413,
            422,
            notes={
                400: "The body is not such a request, or `inputs_version` is not a version "
                "of the branch or of `main` (`derived.bad_inputs_version`)",
                403: "The rows are given as observed (`derived.observed_refused`)",
                404: "The source is neither a function nor a model (`derived.unknown_source`), "
                "or the branch or the type does not exist",
                409: "Assumed or simulated rows for `main` (`derived.live_refused`), or a "
                "property that is filled from a source (`derived.mapped_property`)",
                413: "More rows or bytes than a request may hold (`derived.too_large`)",
                422: "The rows break a constraint of the ontology",
            },
        ),
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": DerivedRequest.model_json_schema()}},
        }
    },
)
async def write_derived(request: Request, services: ServicesDep) -> Response:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > derived.MAX_BYTES:
        raise derived.too_large(
            f"a request holds at most {derived.MAX_BYTES} bytes; this one declares {declared}",
            bytes=int(declared),
        )
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > derived.MAX_BYTES:
            # Nothing more is read: what the body would have said is not looked at.
            raise derived.too_large(
                f"a request holds at most {derived.MAX_BYTES} bytes", bytes=size
            )
        chunks.append(chunk)

    def work() -> Response:
        try:
            asked = DerivedRequest.model_validate_json(b"".join(chunks))
        except ValidationError as exc:
            problems = [
                {"loc": [str(part) for part in error.get("loc", ())], "msg": error.get("msg", "")}
                for error in exc.errors(include_url=False, include_input=False)
            ]
            first = problems[0] if problems else {"loc": [], "msg": "invalid request"}
            where = ".".join(first["loc"])
            log.info("derived rows refused: invalid request", count=len(problems))
            raise ApiError(
                f"invalid request: {where}: {first['msg']}" if where else first["msg"],
                code="api.bad_request",
                detail={"problems": problems},
            ) from exc
        version = derived.write(
            services,
            branch=asked.branch,
            type_name=asked.type,
            rows=asked.rows,
            kind=asked.kind,
            source=asked.source,
            inputs_version=asked.inputs_version,
            confidence=asked.confidence,
            t_valid=asked.t_valid,
            note="derived rows",
        )
        return json_response(
            {"version": version_json(version), "rows": len(asked.rows)}, version, status=201
        )

    return await anyio.to_thread.run_sync(work)
