"""Unattended collectors.

These run on a scheduler on a small always-on cloud VM, as a standalone
entrypoint entirely separate from the MCP server process. There is no chat
session and no human present, so the log and ``collector_runs`` are the only
account of what happened.

Option-chain history cannot be retrieved retrospectively from IBKR. A snapshot
not taken is gone permanently, which is why these come before every analysis
feature. Futures curves ARE reconstructable from per-contract history — see
``curve_collector`` — so only the chain collector carries that absolute.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from .runlog import CollectorRun, RunStatus
from .source import Bar, FuturesContract, MarketData, OptionExpiry, Quote

__all__ = [
    "Bar",
    "CollectorRun",
    "FuturesContract",
    "MarketData",
    "OptionExpiry",
    "Quote",
    "RunStatus",
]
