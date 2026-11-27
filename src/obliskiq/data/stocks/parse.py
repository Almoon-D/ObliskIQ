"""The stock sources' pure parsers: the SEC's listings and SIC, and Yahoo's answer chained into
bars. Read by the collectors and by the replay's loader (H15), which reaches nothing of the broker
through them.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Final

from ...config.schema_sources import StockListingsConfig
from ...domain.stocks import StockBar, StockListing
from ...errors import UpstreamError
from ...research.data import YahooBar

#: The fields the SEC's file names, in the order it serves them (2026-11-17).
FIELDS: Final = ("cik", "name", "ticker", "exchange")


def parse_listings(body: Any, config: StockListingsConfig) -> list[StockListing]:
    """The common stocks the file lists on the kept exchanges, in the file's order."""
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise UpstreamError("the SEC's ticker file is not {fields, data}")
    fields = body.get("fields")
    if not isinstance(fields, list) or tuple(fields) != FIELDS:
        raise UpstreamError(f"the SEC's ticker file names its fields {fields!r}, not {FIELDS}")
    keep = re.compile(config.ticker_pattern)
    drop = re.compile(config.excluded_pattern)
    found: list[StockListing] = []
    for row in body["data"]:
        if not isinstance(row, list) or len(row) != len(FIELDS):
            raise UpstreamError(f"the SEC's ticker file holds a row of another shape: {row!r}")
        cik, name, ticker, exchange = row
        venue = config.exchanges.get(exchange) if isinstance(exchange, str) else None
        if venue is None or not isinstance(ticker, str) or not isinstance(cik, int):
            continue
        if not keep.fullmatch(ticker) or drop.search(ticker):
            continue
        found.append(
            StockListing(
                cik=cik,
                ticker=ticker,
                name=str(name).strip() or ticker,
                exchange=exchange,
                venue=venue,
            )
        )
    return found


def parse_sic(body: Any) -> int | None:
    """A company's SIC from its submissions; ``None`` where the SEC states none."""
    if not isinstance(body, dict) or "sic" not in body:
        raise UpstreamError("the company's submissions carry no sic field")
    text = str(body["sic"] or "").strip()
    if not text:
        return None
    if not text.isdigit():
        raise UpstreamError(f"the company's SIC {text!r} is not a number")
    return int(text)


def parse_incorporation(body: Any) -> str | None:
    """A company's EDGAR incorporation code from its submissions; ``None`` where the SEC states
    none or states one outside EDGAR's two-character codes."""
    if not isinstance(body, dict):
        raise UpstreamError("the company's submissions are not an object")
    text = str(body.get("stateOfIncorporation") or "").strip().upper()
    return text if re.fullmatch(r"[A-Z0-9]{2}", text) else None


def chain(
    answer: Sequence[YahooBar], *, symbol: str, after: date | None, until: date, known_at: datetime
) -> list[StockBar]:
    """The sessions of an answer after ``after`` and up to ``until``, each with its change from
    the answer's session before it."""
    bars: list[StockBar] = []
    previous: YahooBar | None = None
    for bar in answer:
        if bar.session > until:
            break
        if after is None or bar.session > after:
            change = None if previous is None else bar.adj_close / previous.adj_close - 1
            bars.append(
                StockBar(
                    symbol=symbol,
                    session=bar.session,
                    close=bar.close,
                    volume=bar.volume,
                    change=change,
                    known_at=known_at,
                )
            )
        previous = bar
    return bars
