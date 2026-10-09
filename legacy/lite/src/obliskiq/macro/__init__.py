"""Macro data access.

The two public names here are re-exported LAZILY, and that is load-bearing rather
than a style choice. ``RiskFreeRateProvider`` lives in :mod:`obliskiq.macro.rates`,
which imports ``httpx`` because fetching is its job. An eager re-export meant that
importing ANY module in this package — including the network-free
:mod:`obliskiq.macro.series` — pulled an HTTP client into the graph.

The compute unit runs with ``PrivateNetwork=true``, and
``tests/test_compute_isolation.py`` refuses an import graph that so much as names a
network client, whether or not anything calls it. Since the compute layer reads the
stored risk-free rate through ``series``, an eager import here put httpx in that
graph through the package door. PEP 562 keeps both true: the names resolve for
anyone who asks for them, and nothing is imported for anyone who does not.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from typing import Any

__all__ = ["RiskFreeRate", "RiskFreeRateProvider"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from . import rates

        return getattr(rates, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
