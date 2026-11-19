"""Whether the runtime image a manifest names reads the version the manifest is written in.

The toolkit and the runtime image are released apart. An image older than the
manifest would refuse it only once the stack had been started for it, so the
commands that start the stack ask the image first, in one short-lived container
of its own, and refuse before anything is started.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest

from .output import Session
from .stack import RUNTIME_COMMAND, Stack, StackError

log = Log("runtimecheck")

TOO_OLD = "runtime.too_old"
# What an image from before the runtime could say what it reads is taken to read.
UNVERSIONED = 1
_ASK_TIMEOUT_S = 120.0
# How a command line the image does not know ends: its usage error.
_USAGE_EXIT = 2
_UNKNOWN_COMMAND = "No such command"

# Asks an image what it is; None when the image knows no `version` command.
ImageAnswer = Callable[[Stack, str], dict[str, Any] | None]


def ask_image(stack: Stack, image: str) -> dict[str, Any] | None:
    """What `dta-runtime version` prints in a container of `image`."""
    try:
        return stack.run_image(image, RUNTIME_COMMAND, "version", timeout=_ASK_TIMEOUT_S)
    except StackError as exc:
        detail = exc.detail or {}
        unknown = (
            exc.code == "stack.bad_output"
            and detail.get("exit_code") == _USAGE_EXIT
            and _UNKNOWN_COMMAND in str(detail.get("stderr") or "")
        )
        if not unknown:
            raise
        log.warn(
            "the runtime image knows no version command; it is taken to read version 1",
            image=image,
        )
        return None


# The one place the toolkit asks an image; the tests put the image's answer here.
answer: ImageAnswer = ask_image


def image_reads(current: Session, stack: Stack) -> int:
    """The newest manifest version the stack's runtime image reads; asked once per command."""
    image = stack.plan.runtime_image
    known = current.image_reads.get(image)
    if known is not None:
        log.debug("runtime image already asked in this command", image=image, reads=known)
        return known
    started = time.perf_counter()
    try:
        document = answer(stack, image)
    except DtaError as exc:
        log.error("the runtime image could not be asked what it reads", exc=exc, image=image)
        raise
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    reads = UNVERSIONED
    if document is not None:
        found = document.get("manifest_version")
        if isinstance(found, int) and not isinstance(found, bool) and found >= 1:
            reads = found
        else:
            log.warn(
                "the runtime image's version names no manifest version; it is taken to read "
                "version 1",
                image=image,
                answered=document,
            )
    log.info(
        "runtime image reads manifest versions up to",
        image=image,
        reads=reads,
        version=(document or {}).get("version"),
        elapsed_ms=elapsed_ms,
    )
    current.image_reads[image] = reads
    return reads


def require_image_reads(current: Session, stack: Stack, manifest: Manifest) -> None:
    """Refuse when the manifest is newer than the stack's runtime image reads."""
    reads = image_reads(current, stack)
    written = manifest.version
    if written <= reads:
        return
    image = stack.plan.runtime_image
    log.error(
        "the runtime image is older than the manifest",
        image=image,
        manifest_version=written,
        image_reads=reads,
    )
    raise DtaError(
        f"the manifest is written in manifest version {written}, but the runtime image "
        f"{image} reads versions up to {reads}: name a newer runtime image in runtime.image "
        f"(one that reads version {written})",
        code=TOO_OLD,
        detail={
            "manifest_version": written,
            "image": image,
            "image_reads": reads,
            "next": f"set runtime.image to an image that reads manifest version {written}",
        },
    )
