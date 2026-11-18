"""The build skill's tests read the skill's text and check it against the installed tool."""

from __future__ import annotations

import sys
from pathlib import Path

# The helper modules next to this file are imported by name from the tests.
sys.path.insert(0, str(Path(__file__).parent))
