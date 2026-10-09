"""The tools, grouped by what they read.

Registration order is the order a session should call them in: the sleeve first,
because the budget defines the universe and discovering it last is the most
repeated failure mode in this account; the ledger writes last, because nothing
should be written before everything has been read.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..context import ServerContext
from . import (
    catalysts,
    commodity,
    fx,
    grid,
    ledger,
    marketdata,
    operations,
    portfolio,
    risk,
    screen,
    sleeve,
)

#: Every module that registers tools, in call order.
MODULES = (
    sleeve,
    portfolio,
    risk,
    grid,
    fx,
    commodity,
    screen,
    catalysts,
    marketdata,
    ledger,
    operations,
)


def register_all(mcp: FastMCP, context: ServerContext) -> None:
    for module in MODULES:
        module.register(mcp, context)


__all__ = ["MODULES", "register_all"]
