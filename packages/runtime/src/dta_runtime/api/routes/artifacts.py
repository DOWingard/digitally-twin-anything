"""Files kept outside the graph by the SHA-256 of their content: code, fitted states, tables."""

from __future__ import annotations

from typing import Annotated

import anyio.to_thread
from dta_core.log import Log
from fastapi import APIRouter, Path, Request
from starlette.responses import FileResponse, Response

from ...artifacts import MAX_BYTES
from ..deps import ServicesDep
from ..docs import API_PREFIX, errors
from ..formats import json_response
from ..models import ArtifactStored

log = Log("api.artifacts")

router = APIRouter(prefix=f"{API_PREFIX}/artifacts", tags=["artifacts"])

BINARY = "application/octet-stream"
Sha256 = Annotated[
    str,
    Path(description="SHA-256 of the content, 64 hexadecimal characters", examples=["ab" * 32]),
]


@router.post(
    "",
    summary="Keep a file by the SHA-256 of its content",
    description="The body is the file, whatever its kind. The same content is kept once: "
    "sending it again answers 200 with the same hash. Nothing is recorded in the graph; a "
    f"registration that refers to the file records it. At most {MAX_BYTES} bytes.",
    response_model=ArtifactStored,
    status_code=201,
    responses={
        200: {"model": ArtifactStored, "description": "The same content was already kept"},
        **errors(
            400,
            413,
            notes={400: "The body is empty", 413: "The body is larger than an artifact may be"},
        ),
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {BINARY: {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def keep(request: Request, services: ServicesDep) -> Response:
    upload = await anyio.to_thread.run_sync(services.artifacts.upload)
    try:
        async for chunk in request.stream():
            if chunk:
                await anyio.to_thread.run_sync(upload.write, chunk)
        stored = await anyio.to_thread.run_sync(upload.commit)
    except BaseException:
        # Also when the client went away half way: nothing half written stays.
        await anyio.to_thread.run_sync(upload.discard)
        raise
    document = {
        "sha256": stored.sha256,
        "size_bytes": stored.size_bytes,
        "location": stored.location,
    }
    return json_response(document, status=201 if stored.created else 200)


@router.get(
    "/{sha256}",
    summary="The file kept under a hash",
    response_class=FileResponse,
    responses={
        200: {"content": {BINARY: {"schema": {"type": "string", "format": "binary"}}}},
        **errors(404, notes={404: "No file is kept under this hash"}),
    },
)
def fetch(services: ServicesDep, sha256: Sha256) -> Response:
    path = services.artifacts.find(sha256)
    return FileResponse(path, media_type=BINARY)
