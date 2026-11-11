from pathlib import Path

import pytest
import yaml
from dta_core.ontology import load_ontology

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def water_path() -> Path:
    return FIXTURES / "water_network.yaml"


@pytest.fixture(scope="session")
def water(water_path):
    return load_ontology(water_path)


@pytest.fixture
def water_document(water_path) -> dict:
    """A fresh, mutable copy of the water network ontology."""
    return yaml.safe_load(water_path.read_text(encoding="utf-8"))


@pytest.fixture
def write_schema(tmp_path):
    """Write a LinkML document to a file and return its path."""
    counter = iter(range(1_000))

    def write(document: dict) -> Path:
        path = tmp_path / f"schema_{next(counter)}.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return path

    return write
