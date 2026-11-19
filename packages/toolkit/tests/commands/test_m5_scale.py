"""The worker image of the live reference twin: its full requirements built on the runtime
image, the image's user able to import them under a read-only root. Run with `-m scale`.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from dta_toolkit import workerimage
from m3tk_kit import RUNTIME_IMAGE, needs_stack

REQUIREMENTS = Path(__file__).parents[4] / "testbed" / "twin-live" / "worker-requirements.txt"
TWIN_ID = "m5tkref"
BUILD_LIMIT_S = 900.0
IMPORT_LIMIT_S = 60.0


@needs_stack
@pytest.mark.scale
@pytest.mark.skipif(
    not REQUIREMENTS.is_file(),
    reason="the internal testbed is not in this tree",
)
def test_the_reference_twins_worker_image_imports_its_packages() -> None:
    requirements = REQUIREMENTS.read_bytes()
    runtime_id = workerimage.image_id(RUNTIME_IMAGE)
    assert runtime_id is not None
    tag = workerimage.worker_tag(TWIN_ID, requirements, runtime_id)
    assert tag.startswith(f"dta-worker-{TWIN_ID}:")
    try:
        started = time.perf_counter()
        # Not from the builder's cache: the time is that of downloading and installing.
        workerimage.build_image(
            tag, TWIN_ID, RUNTIME_IMAGE, requirements, timeout=BUILD_LIMIT_S, fresh=True
        )
        build_s = time.perf_counter() - started
        size = workerimage.image_size(tag)
        runtime_size = workerimage.image_size(RUNTIME_IMAGE)
        print(f"\n{tag}: built in {build_s:.1f} s, {size} bytes ({runtime_size} for the runtime)")
        started = time.perf_counter()
        done = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--read-only",
                "--tmpfs",
                "/tmp",
                "--network",
                "none",
                "--entrypoint",
                "python",
                tag,
                "-c",
                "import os, wntr; print(os.getuid(), wntr.__version__)",
            ],
            capture_output=True,
            text=True,
            timeout=IMPORT_LIMIT_S,
            check=False,
        )
        import_s = time.perf_counter() - started
        print(f"import wntr as the image's user: {import_s:.1f} s, {done.stdout.strip()}")
        assert done.returncode == 0, done.stderr[-2000:]
        uid, version = done.stdout.split()
        assert uid != "0" and version == "1.5.0"
        assert build_s <= BUILD_LIMIT_S
    finally:
        # By its tag, which names only this test's image.
        subprocess.run(["docker", "image", "rm", tag], capture_output=True, check=False)
    assert workerimage.image_id(tag) is None
