"""Yahoo Finance's symbol for an IBKR listing, shared by the research loader and the earnings
collector.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping


def yahoo_symbol(
    symbol: str, exchange: str, suffixes: Mapping[str, str], digits: Mapping[str, int]
) -> str | None:
    """Yahoo's symbol for ``symbol`` on the IBKR listing ``exchange``; ``None`` where Yahoo's
    suffix for the exchange is not configured. A share class's dot or space is Yahoo's dash
    (``RCI.B`` is ``RCI-B``), a trailing dot is dropped (``NG.``), and a venue whose numeric
    codes Yahoo pads (``digits``) is padded (Hong Kong's ``700`` is ``0700``)."""
    suffix = suffixes.get(exchange)
    if suffix is None:
        return None
    cleaned = symbol.strip().rstrip(".").replace(" ", "-").replace(".", "-")
    width = digits.get(exchange)
    if width is not None and cleaned.isdigit():
        cleaned = cleaned.zfill(width)
    return f"{cleaned}{suffix}"
