"""Tolerant numeric coercion.

IBKR returns strikes as strings and prices with thousands separators; both need casting
before arithmetic. Ported from ObliskIQ-lite-MCP (which took it from IBKR-Portmanteau).

* ``default=None`` lets a caller distinguish "absent" from "zero": collapsing both to 0.0
  silently turns a missing bid into a real one.
* A trailing ``%`` is stripped. IBKR returns implied vol as a percent-suffixed string in
  some contexts; without this every such IV would read as *absent*, which writes no row
  and leaves no trace. Whether ``31.42`` means 31.42% or 0.3142 is a units question for
  the caller.
* **A non-finite result is treated as absent, never as a measurement.** ``nan``, ``inf``
  and ``-inf`` all parse under Python's ``float()`` (and ``1e999`` overflows to ``inf``),
  so an upstream serving any of them would otherwise reach the store as a real quote. This
  is the single coercion point, so the guard sits here once.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from typing import Any, overload


@overload
def safe_float(val: Any, default: float = ...) -> float: ...
@overload
def safe_float(val: Any, default: None) -> float | None: ...


def safe_float(val: Any, default: float | None = 0.0) -> float | None:
    """Coerce to float, returning ``default`` when the value is absent, unparseable, or not
    finite.

    Handles the shapes IBKR and the free macro APIs actually return: ``None``, the empty
    string, FRED's ``"."`` placeholder, thousands separators, and the ``C``/``H``/``L``
    prefixes IBKR puts on snapshot fields (previous close, halted bounds).
    """
    if val is None:
        return default
    if isinstance(val, bool):
        return float(val)
    if isinstance(val, int | float):
        return _finite_or(float(val), default)
    text = str(val).strip().replace(",", "")
    if not text or text == ".":
        return default
    if text.endswith("%"):
        text = text[:-1].strip()
        if not text:
            return default
    if text[0] in "CHL" and len(text) > 1:
        text = text[1:]
    try:
        return _finite_or(float(text), default)
    except ValueError, TypeError:
        return default


def _finite_or(value: float, default: float | None) -> float | None:
    """``value`` unless it is ``nan`` or an infinity, in which case ``default``."""
    return value if math.isfinite(value) else default


def safe_int(val: Any, default: int | None = 0) -> int | None:
    """Coerce to int via :func:`safe_float`, so ``"1,234.0"`` parses."""
    as_float = safe_float(val, None)
    if as_float is None:
        return default
    # safe_float returns only finite floats, which int() always converts.
    return int(as_float)
