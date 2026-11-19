"""A twin with code of its own, through the launcher: `docker build` from inside the toolkit.

The toolkit builds the worker's image from a build context in its own /tmp,
which the docker client in the toolkit's container sends to the host's daemon;
the twin's code is packed from the mounted workspace and uploaded to the API on
the loopback address. Its limit is fixed here before any measurement.
"""

from __future__ import annotations

import time

import launcher_kit as kit
from sample_twin import WORKER_PREFIX, Twin, expect

pytestmark = [kit.needs_image, kit.needs_runtime]

FLOW_LIMIT_S = 300.0


def test_twin_with_its_own_code_builds_its_worker_image(make_twin) -> None:
    twin: Twin = make_twin(code=True)
    started = time.perf_counter()
    twin.prepare()
    assert twin.gates() == expect(gaps="passed")

    built, _seen, _container = twin.build_watched()
    steps = {entry["step"]: entry for entry in built["steps"]}
    image = steps["image"]
    assert image["status"] == "ok", image
    assert image["detail"]["action"] == "built"
    tag = image["detail"]["tag"]
    assert tag.startswith(WORKER_PREFIX) and twin.worker_images() == [tag]
    assert steps["code"]["status"] == "ok", steps["code"]
    assert steps["functions"]["status"] == "ok", steps["functions"]
    assert steps["api"]["detail"]["worker"]["health"] == "healthy"
    assert twin.gates() == expect(build="passed")

    # A second build finds the image by its tag and builds nothing.
    again = {entry["step"]: entry for entry in twin.ok("build")["steps"]}
    assert again["image"]["detail"]["action"] == "reused"

    accepted = twin.ok("accept")
    assert accepted["passed"] is True and accepted["failed"] == []

    assert twin.ok("down", "--destroy-data")["volumes_removed"] is True
    assert kit.leftovers(twin.project) == {"containers": [], "volumes": [], "networks": []}
    flow_s = time.perf_counter() - started
    print(f"\ntwin with a worker image through the launcher {flow_s:.1f} s")
    assert flow_s <= FLOW_LIMIT_S
