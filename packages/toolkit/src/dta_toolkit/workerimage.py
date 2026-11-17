"""The image the twin's worker runs when its code needs packages the runtime lacks.

It is the runtime image the manifest names with the workspace's requirements
file installed on top. Its tag, `dta-worker-<twin id>:<hash>`, is the first
twelve hexadecimal digits of the SHA-256 of the requirements file's bytes and
the runtime image's id, so the same file on the same runtime image gives the
same tag and a tag that exists is never built again. The tag the last build
made is kept in `.dta/worker-image.json`, which is where the stack's compose
file takes the worker's image from; the image holds no data of the twin, and
removing the stack leaves it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from importlib import resources
from pathlib import Path
from typing import Any

from dta_core.errors import CheckFailed, DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest
from jinja2 import Environment, StrictUndefined

from .workspace import Workspace, now

log = Log("worker_image")

TAG_PREFIX = "dta-worker-"
HASH_DIGITS = 12
RECORD_FILE = "worker-image.json"
REQUIREMENTS_NAME = "requirements.txt"
BUILD_TIMEOUT_S = 1_800.0
_INSPECT_TIMEOUT_S = 60.0
_TEMPLATE = "templates/worker/Dockerfile.j2"
_OUTPUT_TAIL = 4_000
# What `docker` needs from the caller's environment, and nothing else.
_PASSED_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "SSH_AUTH_SOCK",
)

REQUIREMENTS_MISSING = "image.requirements_missing"
RUNTIME_MISSING = "image.runtime_missing"
BUILD_FAILED = "image.build_failed"
DOCKER_UNAVAILABLE = "image.docker_unavailable"


def worker_tag(twin_id: str, requirements: bytes, runtime_image_id: str) -> str:
    """`dta-worker-<twin id>:<hash>` of a requirements file on a runtime image."""
    digest = hashlib.sha256(requirements + b"\0" + runtime_image_id.encode("utf-8")).hexdigest()
    return f"{TAG_PREFIX}{twin_id}:{digest[:HASH_DIGITS]}"


def record_path(workspace: Workspace) -> Path:
    return workspace.meta_dir / RECORD_FILE


def requirements_path(workspace: Workspace, manifest: Manifest) -> Path | None:
    """The requirements file the manifest names, inside the workspace; None without one."""
    relative = manifest.runtime.worker_requirements
    if not relative:
        return None
    path = (workspace.root / relative).resolve()
    if workspace.root not in path.parents:
        log.error("runtime.worker_requirements leads out of the workspace", file=relative)
        raise CheckFailed(
            f"runtime.worker_requirements ({relative}) is outside the workspace",
            code=REQUIREMENTS_MISSING,
            detail={"worker_requirements": relative},
        )
    return path


def recorded_tag(workspace: Workspace, manifest: Manifest) -> str | None:
    """The worker image the last build made for this twin; None when there is none."""
    path = requirements_path(workspace, manifest)
    record = record_path(workspace)
    if path is None or not record.is_file() or not path.is_file():
        return None
    try:
        found = json.loads(record.read_text(encoding="utf-8"))
        current = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError) as exc:
        log.warn("the worker image record cannot be read", error=type(exc).__name__)
        return None
    tag = found.get("tag") if isinstance(found, dict) else None
    if not isinstance(tag, str) or found.get("twin_id") != (
        manifest.twin.id if manifest.twin else None
    ):
        log.warn("the worker image on record is not this twin's; the worker runs runtime.image")
        return None
    if found.get("requirements_sha256") != current:
        # The image last built still runs the code last registered; the next build makes
        # the image for the requirements as they are now.
        log.warn(
            "the requirements changed since the worker image was built; it is used until the "
            "twin is built again",
            tag=tag,
        )
    return tag


def _docker(args: list[str], *, timeout: float, cwd: Path | None = None):
    if shutil.which("docker") is None:
        log.error("docker is not installed or not on the PATH")
        raise DtaError("the docker command is not available", code=DOCKER_UNAVAILABLE)
    environment = {name: os.environ[name] for name in _PASSED_ENV if name in os.environ}
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
            cwd=cwd,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        log.error("docker command timed out", args=args[:2], timeout_s=timeout)
        raise DtaError(
            f"`docker {args[0]}` did not finish within {timeout:g} s",
            code=BUILD_FAILED,
            detail={"timeout_s": timeout},
        ) from exc
    except OSError as exc:
        log.error("docker could not be started", exc=exc)
        raise DtaError(
            f"docker could not be started: {type(exc).__name__}", code=DOCKER_UNAVAILABLE
        ) from exc


def image_id(reference: str) -> str | None:
    """The id of a local image, or None when this machine does not have it."""
    done = _docker(
        ["image", "inspect", "--format", "{{.Id}}", reference], timeout=_INSPECT_TIMEOUT_S
    )
    if done.returncode != 0:
        log.debug("image not on this machine", image=reference)
        return None
    return done.stdout.strip() or None


def image_size(reference: str) -> int | None:
    done = _docker(
        ["image", "inspect", "--format", "{{.Size}}", reference], timeout=_INSPECT_TIMEOUT_S
    )
    if done.returncode != 0:
        return None
    text = done.stdout.strip()
    return int(text) if text.isdigit() else None


def render_dockerfile(twin_id: str, runtime_image: str) -> str:
    template = resources.files("dta_toolkit").joinpath(_TEMPLATE).read_text(encoding="utf-8")
    environment = Environment(
        undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True
    )
    return environment.from_string(template).render(
        twin_id=twin_id, runtime_image=runtime_image, requirements_name=REQUIREMENTS_NAME
    )


def _pip_lines(output: str) -> list[str]:
    """What pip said went wrong, as plain lines without the builder's step prefixes."""
    errors = []
    for raw in output.splitlines():
        # BuildKit prefixes each line of a step with `#<n> <seconds> `.
        text = raw.split(" ", 2)[2] if raw.startswith("#") and raw.count(" ") >= 2 else raw
        text = text.strip()
        if text.startswith("ERROR:") and text not in errors:
            errors.append(text)
    # The builder ends with its own summary of the failed step; pip's lines say why.
    own = [
        text for text in errors if "failed to solve" not in text and "failed to build" not in text
    ]
    return own or errors


def build_image(
    tag: str,
    twin_id: str,
    runtime_image: str,
    requirements: bytes,
    *,
    timeout: float,
    fresh: bool = False,
) -> dict[str, Any]:
    """Build the worker image under `tag`; refused with pip's own message when it fails.

    `fresh` builds every layer again rather than from the builder's cache, so the
    packages are downloaded and installed anew.
    """
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="dta-worker-") as folder:
        context = Path(folder)
        (context / REQUIREMENTS_NAME).write_bytes(requirements)
        (context / "Dockerfile").write_text(
            render_dockerfile(twin_id, runtime_image), encoding="utf-8"
        )
        done = _docker(
            [
                "build",
                "--progress=plain",
                *(["--no-cache"] if fresh else []),
                "--tag",
                tag,
                "--file",
                "Dockerfile",
                ".",
            ],
            timeout=timeout,
            cwd=context,
        )
    elapsed = round((time.perf_counter() - started) * 1000.0, 1)
    if done.returncode != 0:
        output = (done.stderr or "") + (done.stdout or "")
        said = _pip_lines(output)
        log.error(
            "the worker image could not be built",
            tag=tag,
            exit_code=done.returncode,
            pip=said,
            duration_ms=elapsed,
        )
        reason = "; ".join(said) if said else output.strip()[-_OUTPUT_TAIL:]
        raise CheckFailed(
            f"the worker image could not be built: {reason}. Correct runtime.worker_requirements "
            "(every package needs a wheel for this platform and Python 3.12) and build again",
            code=BUILD_FAILED,
            detail={"tag": tag, "pip": said, "output": output[-_OUTPUT_TAIL:]},
        )
    log.info("worker image built", tag=tag, duration_ms=elapsed)
    return {"build_ms": elapsed}


def ensure(
    workspace: Workspace, manifest: Manifest, *, timeout: float = BUILD_TIMEOUT_S
) -> dict[str, Any]:
    """The worker image for the requirements file as it is now: reused when its tag
    exists, built otherwise; recorded for the stack either way."""
    path = requirements_path(workspace, manifest)
    assert path is not None and manifest.twin is not None and manifest.twin.id
    if not path.is_file():
        log.error("the requirements file does not exist", file=manifest.runtime.worker_requirements)
        raise CheckFailed(
            f"runtime.worker_requirements names {manifest.runtime.worker_requirements}, which "
            "does not exist in the workspace",
            code=REQUIREMENTS_MISSING,
            detail={"worker_requirements": manifest.runtime.worker_requirements},
        )
    requirements = path.read_bytes()
    runtime_image = manifest.runtime.image
    runtime_id = image_id(runtime_image)
    if runtime_id is None:
        log.error("the runtime image is not on this machine", image=runtime_image)
        raise CheckFailed(
            f"the runtime image {runtime_image} (runtime.image) is not on this machine; build "
            "or pull it first",
            code=RUNTIME_MISSING,
            detail={"runtime_image": runtime_image},
        )
    tag = worker_tag(manifest.twin.id, requirements, runtime_id)
    result: dict[str, Any] = {
        "tag": tag,
        "runtime_image": runtime_image,
        "runtime_image_id": runtime_id,
        "requirements": manifest.runtime.worker_requirements,
        "requirements_sha256": hashlib.sha256(requirements).hexdigest(),
    }
    if image_id(tag) is not None:
        log.info("the worker image exists; it is not built again", tag=tag)
        result["action"] = "reused"
    else:
        result.update(
            build_image(tag, manifest.twin.id, runtime_image, requirements, timeout=timeout)
        )
        result["action"] = "built"
    result["size_bytes"] = image_size(tag)
    record = {
        "twin_id": manifest.twin.id,
        "tag": tag,
        "runtime_image": runtime_image,
        "runtime_image_id": runtime_id,
        "requirements_sha256": result["requirements_sha256"],
        "at": now().isoformat(),
    }
    with workspace.exclusive():
        target = record_path(workspace)
        known = target.read_text(encoding="utf-8") if target.is_file() else ""
        same = False
        if known:
            try:
                same = {k: v for k, v in json.loads(known).items() if k != "at"} == {
                    k: v for k, v in record.items() if k != "at"
                }
            except ValueError:
                log.warn("the worker image record was not JSON; it is written again")
        if not same:
            workspace.atomic_write(target, json.dumps(record, indent=2) + "\n")
    return result


__all__ = [
    "BUILD_FAILED",
    "RECORD_FILE",
    "TAG_PREFIX",
    "build_image",
    "ensure",
    "image_id",
    "image_size",
    "recorded_tag",
    "render_dockerfile",
    "requirements_path",
    "worker_tag",
]
