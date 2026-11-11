"""Conversion between units given as UCUM codes, reduced to a factor and an offset.

The conversion itself is pint's. It is worked out once per property and
applied to whole columns as `value * factor + offset`, so no value passes
through Python.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cache, lru_cache
from typing import Any

from dta_core.log import Log

log = Log("sync.units")

# Far enough from zero that the slope of a conversion with an offset is not
# lost in the subtraction, and a power of two so the division is exact.
_SLOPE_PROBE = float(2**20)
_LINEARITY_PROBE = 3.0
_TOLERANCE = 1e-9


class UnitProblem(ValueError):
    """A unit code is unknown, or two units cannot be converted into each other."""


@dataclass(frozen=True)
class Affine:
    """`target = source * factor + offset`."""

    factor: float
    offset: float

    @property
    def is_identity(self) -> bool:
        return self.factor == 1.0 and self.offset == 0.0

    def apply(self, value: float) -> float:
        return value * self.factor + self.offset


@cache
def _registry() -> Any:
    # Building the registry parses pint's unit tables; done once, on first use.
    from ucumvert import PintUcumRegistry

    return PintUcumRegistry()


def _parse(code: str) -> Any:
    try:
        return _registry().from_ucum(code)
    except Exception as exc:
        log.debug("unit code refused", code=code, error=type(exc).__name__)
        raise UnitProblem(f"{code!r} is not a UCUM unit code") from exc


def check_code(code: str) -> None:
    """Raise `UnitProblem` unless `code` is a UCUM unit code."""
    _parse(code)


@lru_cache(maxsize=1024)
def conversion(source: str, target: str) -> Affine:
    """How a value in `source` becomes a value in `target`.

    Raises `UnitProblem` when either code is unknown, when the two measure
    different things, or when the conversion is not a straight line (a
    logarithmic unit, say).
    """
    if source == target:
        return Affine(1.0, 0.0)
    given, wanted = _parse(source), _parse(target)
    registry = _registry()

    def image(value: float) -> float:
        # A code may carry a number of its own (`10*3/uL`), which is part of the unit.
        quantity = registry.Quantity(value * given.magnitude, given.units)
        return float(quantity.to(wanted.units).magnitude) / float(wanted.magnitude)

    try:
        offset = image(0.0)
        if offset == 0.0:
            factor = image(1.0)
        else:
            factor = (image(_SLOPE_PROBE) - offset) / _SLOPE_PROBE
        probe = image(_LINEARITY_PROBE)
    except Exception as exc:
        log.debug(
            "units cannot be converted", source=source, target=target, error=type(exc).__name__
        )
        raise UnitProblem(f"{source!r} cannot be converted to {target!r}") from exc
    if not (math.isfinite(factor) and math.isfinite(offset)) or factor == 0.0:
        raise UnitProblem(f"{source!r} cannot be converted to {target!r}")
    expected = _LINEARITY_PROBE * factor + offset
    if not math.isclose(probe, expected, rel_tol=_TOLERANCE, abs_tol=_TOLERANCE):
        raise UnitProblem(
            f"the conversion from {source!r} to {target!r} is not a factor and an offset"
        )
    return Affine(factor, offset)
