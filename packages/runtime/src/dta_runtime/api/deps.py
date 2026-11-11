"""Dependencies of the route handlers."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from .services import Services


async def get_services(request: Request) -> Services:
    # A coroutine, so that it runs on the event loop without taking a worker
    # thread from the handlers that talk to the store.
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]
