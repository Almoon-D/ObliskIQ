"""Shared helpers ported from IBKR-Portmanteau.

``.http`` is deliberately NOT re-exported here. Importing this package would
otherwise pull httpx into every graph that wanted a date helper — including
``obliskiq-compute``, whose systemd unit runs with ``PrivateNetwork=true`` on the
grounds that it touches no network. Callers that want the HTTP layer say
``from ..util.http import ...``, which every one of them already did.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from .numeric import safe_float, safe_int

__all__ = ["safe_float", "safe_int"]
