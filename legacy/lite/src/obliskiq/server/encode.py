"""Turning engine results into JSON a model can read without being misled.

``dataclasses.asdict`` is not enough on its own. The engines return ``date``
objects, tuples, and — through numpy — floats that can be ``nan`` or ``inf``.
JSON has no representation for any of them, and the failure mode differs by
kind: a date that serialises as ``"2026-08-23"`` is fine, but a NaN that
serialises as a number is a lie in the shape of data, and a caller cannot tell
it from a measurement.

So non-finite floats become ``null``. The layer's whole doctrine is that a
missing value is reported as missing with a reason, never as a plausible number,
and that has to survive the wire.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any


def to_jsonable(value: Any) -> Any:
    """Recursively convert ``value`` into JSON-serialisable primitives."""
    if value is None or isinstance(value, (str, bool, int)):
        return value

    if isinstance(value, float):
        return _finite(value)

    if isinstance(value, (datetime, date, time)):
        return value.isoformat()

    if isinstance(value, Decimal):
        return _finite(float(value))

    if isinstance(value, Enum):
        return to_jsonable(value.value)

    if isinstance(value, Path):
        return str(value)

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        encoded = {
            field.name: to_jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
        encoded.update(_derived(value))
        return encoded

    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}

    if isinstance(value, (set, frozenset)):
        return [to_jsonable(item) for item in sorted(value, key=str)]

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_jsonable(item) for item in value]

    # numpy scalars and anything else that knows how to become a Python scalar.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return to_jsonable(item())
        except (TypeError, ValueError):  # pragma: no cover - defensive
            pass

    if callable(value):
        # Almost always a property or method referenced without being called.
        # Stringifying it would ship "<bound method ...>" to the model as if it
        # were data, so this fails loudly instead.
        raise TypeError(
            f"{value!r} is callable and cannot be serialised. A method or property "
            "was referenced without being called."
        )

    return str(value)


def _derived(value: Any) -> dict[str, Any]:
    """Public ``@property`` values on a dataclass, alongside its fields.

    The engines put their verdicts in properties — ``ZombieAssessment.fires``,
    ``GateResult.passed``, ``CapReport.breaches``, ``FourWay.is_rankable``. Those
    ARE the answer, and encoding fields alone would ship the evidence without the
    conclusion and leave every tool to remember to copy it back in.

    A property that raises is omitted rather than allowed to fail the response:
    it is derived information, and the fields it was derived from are already
    there.
    """
    out: dict[str, Any] = {}
    for klass in type(value).__mro__:
        for name, attribute in vars(klass).items():
            if name.startswith("_") or not isinstance(attribute, property):
                continue
            if name in out:
                continue
            try:
                out[name] = to_jsonable(getattr(value, name))
            except Exception:  # pragma: no cover - a derived value, not the data
                continue
    return out


def _finite(value: float) -> float | None:
    """``nan`` and the infinities are not numbers anybody can act on."""
    return value if math.isfinite(value) else None
