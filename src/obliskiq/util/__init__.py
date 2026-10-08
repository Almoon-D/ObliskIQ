"""Shared helpers: tolerant numeric coercion, UTC time stamps and the HTTP retry loop.

``.http`` is deliberately not re-exported: importing this package must not pull httpx into
code that only wanted a date helper. Callers that want the HTTP layer import
``obliskiq.util.http`` directly.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from .numeric import safe_float, safe_int

__all__ = ["safe_float", "safe_int"]
