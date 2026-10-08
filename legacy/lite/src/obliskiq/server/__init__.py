"""``obliskiq-lite-mcp`` — the computation layer as an MCP server.

The third entrypoint, alongside ``obliskiq-collect`` and ``obliskiq-compute``, and
the only one that binds a port. It reimplements nothing: every tool composes the
engines in :mod:`obliskiq.compute` and the ledger writes in
:mod:`obliskiq.store.ledger`, so a rule has exactly one home.

Named ``server`` rather than ``mcp`` so it never shadows the ``mcp`` SDK package.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from .context import ServerContext

__all__ = ["ServerContext"]
