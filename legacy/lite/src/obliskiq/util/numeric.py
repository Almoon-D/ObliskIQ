"""Tolerant numeric coercion.

Ported from IBKR-Portmanteau's ``_safe_float``. IBKR returns strikes as strings and
prices with thousands separators; both need casting before arithmetic.

Changed from the original: a caller can ask for ``default=None`` to distinguish
"absent" from "zero". The original always collapsed both to 0.0, which silently
turns a missing bid into a real one.

Also handles a trailing ``%``. IBKR returns implied vol as a percent-suffixed
string in some contexts, and without this ``safe_float("31.42%")`` returned the
default — so every IV became *absent* rather than wrong. An absent value raises
no error, writes no row and leaves no trace, which is the worse failure. Stripping
the suffix recovers the number; deciding whether it means 31.42% or 0.3142 is a
units question and belongs to the caller, not here.

**A non-finite result is treated as absent, never as a measurement.** ``nan``,
``inf`` and ``-inf`` all parse cleanly under Python's own ``float()`` — JSON's
grammar admits the literals ``NaN`` and ``Infinity``, and ``1e999`` overflows to
``inf`` rather than raising — so an upstream that serves any of them would
otherwise reach the store as though it were a real quote. Every consumer here
already treats ``default`` as "nobody supplied a usable value"; a non-finite
float is exactly that, dressed as a number. This is the single coercion point
for every collector, so the guard sits here once rather than at each of its
call sites.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import math
from typing import Any, overload


@overload
def safe_float(val: Any, default: float = ...) -> float: ...
@overload
def safe_float(val: Any, default: None) -> float | None: ...


def safe_float(val: Any, default: float | None = 0.0) -> float | None:
    """Coerce to float, returning ``default`` when the value is absent, unparseable,
    or not finite.

    Handles the shapes IBKR and the free macro APIs actually return: ``None``, the
    empty string, FRED's ``"."`` placeholder, thousands separators, and a leading
    ``C`` prefix on IBKR snapshot fields that denote a closing price.
    """
    if val is None:
        return default
    if isinstance(val, bool):
        return float(val)
    if isinstance(val, (int, float)):
        return _finite_or(float(val), default)
    text = str(val).strip().replace(",", "")
    if not text or text == ".":
        return default
    # A trailing % is a formatting artefact. The magnitude it implies is the
    # caller's problem: see PercentAware decoding in ibkr/cp_client.py.
    if text.endswith("%"):
        text = text[:-1].strip()
        if not text:
            return default
    # IBKR prefixes some snapshot values: 'C' = previous close, 'H'/'L' = halted bounds.
    if text[0] in "CHL" and len(text) > 1:
        text = text[1:]
    try:
        return _finite_or(float(text), default)
    except (ValueError, TypeError):
        return default


def _finite_or(value: float, default: float | None) -> float | None:
    """``value`` unless it is ``nan`` or an infinity, in which case ``default``.

    Python's own ``float()`` parses ``"nan"``, ``"inf"``, ``"Infinity"`` and an
    overflowing literal like ``"1e999"`` without complaint, and every caller of
    :func:`safe_float` already reads ``default`` as "unusable" rather than as a
    quote of nothing — so a non-finite parse is folded into the same path a
    missing or garbled value already takes, rather than becoming a distinct third
    outcome nothing here was built to carry.
    """
    return value if math.isfinite(value) else default


def safe_int(val: Any, default: int | None = 0) -> int | None:
    """Coerce to int via safe_float, so ``"1,234.0"`` parses."""
    as_float = safe_float(val, None)
    if as_float is None:
        return default
    try:
        return int(as_float)
    except (ValueError, OverflowError):
        return default
