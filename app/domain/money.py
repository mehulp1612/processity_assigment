"""Money arithmetic done with Decimal, not float.

Rupee/paise amounts must not drift. We compute everything as ``Decimal`` with
explicit ``ROUND_HALF_UP`` (the convention Indian invoices use), and only convert
to ``float`` at the DB boundary. Never do ``0.1 + 0.2`` money math with floats.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Union

Number = Union[int, float, str, Decimal]

_PAISE = Decimal("0.01")
_RUPEE = Decimal("1")


def D(x: Number) -> Decimal:
    """Coerce to Decimal via str() so float artefacts (0.1) don't leak in."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def paise(x: Number) -> Decimal:
    """Round to 2 decimals (nearest paise), half-up."""
    return D(x).quantize(_PAISE, rounding=ROUND_HALF_UP)


def rupee(x: Number) -> Decimal:
    """Round to the nearest whole rupee, half-up (bill-level round-off)."""
    return D(x).quantize(_RUPEE, rounding=ROUND_HALF_UP)


def as_float(x: Decimal) -> float:
    """Convert a Decimal amount to float for storage/JSON at the boundary."""
    return float(x)
