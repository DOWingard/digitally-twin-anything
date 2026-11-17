"""The package is imported by the API and the scheduler: it loads no model library."""

from __future__ import annotations

import subprocess
import sys


def test_the_package_imports_no_model_library():
    code = (
        "import sys\n"
        "import dta_runtime.fidelity\n"
        "heavy = ('pandas', 'statsforecast', 'mlforecast', 'sklearn', 'utilsforecast', "
        "'scipy', 'numba')\n"
        "print(sorted(name for name in heavy if name in sys.modules))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "[]"


def test_the_package_exports_what_its_callers_use():
    import dta_runtime.fidelity as fidelity

    for name in (
        "check",
        "corrected",
        "retry",
        "resolve",
        "clear_signal",
        "open_incident",
        "close_incident",
        "state",
        "suspects",
        "observed_reading",
        "incidents",
        "incident",
        "checks",
        "signals",
        "errors",
        "Predictor",
        "Freshness",
        "CorrectionRequest",
        "CorrectionResult",
        "CheckOutcome",
        "FidelityError",
    ):
        assert hasattr(fidelity, name), name
        assert name in fidelity.__all__, name
