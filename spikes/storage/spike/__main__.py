"""Command line entry point: `python -m spike --engine dolt terminusdb --fixture smoke`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .bench import ENGINES, run
from .config import ConfigError, load_config
from .fixture import FIXTURES
from .log import Log, configure
from .ontology import load_ontology

PROJECT_DIR = Path(__file__).resolve().parents[1]


def _row(result: dict) -> str:
    def cell(op: str) -> str:
        value = result["ops"].get(op, {}).get("p95_ms")
        return f"{value:>9.1f}" if value is not None else f"{'fail':>9}"

    failed = [name for name, gate in result["gates"].items() if not gate["pass"]]
    return (
        f"{result['engine']:<11}{result['fixture']['name']:<7}"
        f"{cell('commit')}{cell('point_read_asof')}{cell('node_table_asof')}"
        f"{cell('property_history')}{cell('neighbors_k2')}{cell('projection_traverse')}"
        f"{cell('branch_create')}  {'PASS' if result['hard_gates_passed'] else 'FAIL'}"
        f"  {', '.join(failed)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="spike", description=__doc__)
    parser.add_argument("--engine", nargs="+", choices=sorted(ENGINES), default=sorted(ENGINES))
    parser.add_argument("--fixture", nargs="+", choices=sorted(FIXTURES), default=["smoke"])
    args = parser.parse_args()

    results_dir = PROJECT_DIR / "results"
    log_path = configure(results_dir)
    log = Log("main")
    try:
        config = load_config()
        ontology = load_ontology(PROJECT_DIR / "ontology" / "fixture.yaml")
    except ConfigError:
        return 2
    except Exception as exc:
        log.error("startup failed", exc=exc)
        return 2

    results, crashed = [], False
    for fixture_name in args.fixture:
        for engine_name in args.engine:
            try:
                results.append(run(engine_name, fixture_name, config, ontology, results_dir))
            except Exception as exc:
                crashed = True
                log.error("benchmark crashed", exc=exc, engine=engine_name, fixture=fixture_name)

    header = (
        f"{'engine':<11}{'fixture':<7}{'commit':>9}{'pt asof':>9}{'table':>9}"
        f"{'history':>9}{'hops k2':>9}{'project':>9}{'branch':>9}  gates (p95 ms)"
    )
    print(header)
    for result in results:
        print(_row(result))
    print(f"log: {log_path}")
    return 1 if crashed else 0


if __name__ == "__main__":
    sys.exit(main())
