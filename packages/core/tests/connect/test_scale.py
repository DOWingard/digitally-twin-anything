"""Full-size timing of the file connector and the profiler (run with `-m scale`).

Thresholds are fixed here, before measuring, and generous because the machine
running the tests may be heavily loaded.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from dta_core.connect import open_connector, profile_source
from dta_core.manifest.models import Source
from dta_core.sources import BATCH_ROWS

ENTITIES = 100
TIMESTAMPS = 10_000
PROFILE_LIMIT_S = 60.0
READ_LIMIT_S = 20.0


def _write_fixture(folder: Path) -> None:
    rng = np.random.default_rng(42)
    rows = ENTITIES * TIMESTAMPS
    base = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1_000_000)
    instants = base + np.tile(np.arange(TIMESTAMPS, dtype=np.int64) * 10_000_000, ENTITIES)
    walk = np.cumsum(rng.normal(0.0, 0.1, (ENTITIES, TIMESTAMPS)), axis=1).reshape(-1)
    table = pa.table(
        {
            "sensor": np.repeat([f"sensor-{k:03d}" for k in range(ENTITIES)], TIMESTAMPS),
            "ts": pa.array(instants, pa.timestamp("us", tz="UTC")),
            "pressure": 50.0 + walk + rng.normal(0.0, 0.2, rows),
            "flow": 10.0 + np.sin(np.arange(rows) / 500.0) + rng.normal(0.0, 0.05, rows),
            "temperature": 15.0 + rng.normal(0.0, 1.0, rows),
        }
    )
    folder.mkdir(parents=True)
    pq.write_table(table, folder / "readings.parquet")


@pytest.mark.scale
def test_profile_and_read_one_million_rows(tmp_path: Path) -> None:
    _write_fixture(tmp_path / "data")
    source = Source(
        connector="file",
        location="file://${DTA_TEST_SCALE_DIR}",
        options={"datasets": {"readings": {"glob": "data/*.parquet", "format": "parquet"}}},
    )
    with open_connector("scale", source, {"DTA_TEST_SCALE_DIR": str(tmp_path)}) as connector:
        load_before = os.getloadavg()
        started = time.perf_counter()
        sizes = [table.num_rows for table in connector.read("readings")]
        read_s = time.perf_counter() - started

        started = time.perf_counter()
        profile = profile_source(connector, profiled_at=datetime(2026, 11, 11, tzinfo=UTC))
        profile_s = time.perf_counter() - started
        load_after = os.getloadavg()

    print(
        f"\nscale: read {read_s:.2f} s (limit {READ_LIMIT_S} s), "
        f"profile {profile_s:.2f} s (limit {PROFILE_LIMIT_S} s), "
        f"load average before {load_before}, after {load_after}"
    )
    assert sum(sizes) == ENTITIES * TIMESTAMPS and max(sizes) <= BATCH_ROWS
    dataset = profile.datasets["readings"]
    assert dataset.rows == ENTITIES * TIMESTAMPS
    assert dataset.entity_columns == ["sensor"]
    assert dataset.time_column == "ts"
    assert dataset.columns["temperature"].noise_sigma is not None
    assert read_s <= READ_LIMIT_S
    assert profile_s <= PROFILE_LIMIT_S
