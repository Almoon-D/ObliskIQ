"""The market-data seam.

Everything the collectors need from a data provider goes through
:class:`MarketData`. There are two implementations: one over the Client Portal
Web API, and one over recorded fixtures. That seam is what makes the collectors
testable in a sandbox with no live IBKR access, and it is also the seam along
which collection would migrate if a CP OAuth session turns out to invalidate the
MCP session for the same account — every row carries ``source``.

Only the CP route is reachable unattended, though not for the reason this said
before. IBKR's Custom Connector serves the MCP tools over HTTPS and answers a
headless container — so the connector is not confined to a chat session. What
stops a scheduled collector using it is the credential: an OAuth 2.0 bearer token
this build cannot yet mint. ``mcp`` remains a legal value of ``source`` for rows
collected that way, through a capture. See ``ibkr/connector.py``.

.. warning::
   Everything IBKR-shaped here is DOCUMENTATION-DERIVED AND UNVERIFIED against
   the Client Portal route. It has to be re-tested against live responses, and
   corrections belong in ``references/ibkr-playbook.md``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..errors import PermanentUpstreamError
from ..logging_setup import get_logger
from ..util.numeric import safe_float, safe_int
from ..util.timeutil import epoch_ms_to_stamp, parse_date

if TYPE_CHECKING:  # pragma: no cover
    # An annotation, not a dependency. Importing obliskiq.ibkr eagerly would
    # put the whole OAuth and httpx stack into every graph that reaches this
    # module — obliskiq-compute among them, and its unit runs with
    # PrivateNetwork=true. The one caller that constructs a CpApiMarketData
    # (ibkr.session.connect_cp_client) already holds the client.
    from ..ibkr.cp_client import CpApiClient

log = get_logger("collect.source")

#: Legal values of the ``source`` column, and what each means.
SOURCE_CP_API = "cp_api"
SOURCE_MCP = "mcp"

#: The consolidated row of ``/portfolio/{id}/ledger``, beside the per-currency
#: ones. It is already in the account's base currency, which is why the NAV is
#: read from here rather than summed across the others at today's FX.
LEDGER_BASE_KEY = "BASE"

#: The ``exchangerate`` a per-currency ledger row carries when it IS the base
#: currency: the rate that converts it to base, which for base itself is one.
#: Compared exactly rather than within a tolerance, because a tolerance here
#: would be a threshold nothing measured — and anything that is not exactly one
#: is a currency the consolidation converted, which is the case being excluded.
#: A near-miss therefore reads as "unidentified" and travels as ``None``, which
#: is the behaviour this identification replaced rather than a new failure.
LEDGER_BASE_RATE = 1.0

#: Security types that can serve as an UNDERLYING root. An option position or a
#: structured note in the account is a holding, not something to collect a chain
#: or a bar series for.
UNDERLYING_SEC_TYPES = frozenset({"STK", "ETF", "FUT", "IND"})


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar. ``ts`` is the bar start as a UTC stamp."""

    ts: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None


@dataclass(frozen=True)
class Quote:
    """A decoded market snapshot.

    Every field is optional and ``None`` means *absent*, never zero: a missing bid
    is not a bid of nothing.
    """

    conid: int
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    implied_vol: float | None = None
    delta: float | None = None
    open_interest: int | None = None
    volume: int | None = None
    #: Underlying-only, and served only when a verified field id exists for it.
    #: See ibkr.cp_api.marketdata.underlying_snapshot_fields.
    historical_vol_30d: float | None = None
    iv_percentile_52w: float | None = None
    #: Underlying-only, DECIMAL, and served only when a verified field id exists.
    #: The route serves a percent under a name that does not say so, so the
    #: conversion happens in the decoder and never at a call site — see
    #: CpApiClient._decode_dividend_yield.
    dividend_yield: float | None = None
    #: The market-data state the route reported for this quote, verbatim and
    #: unnormalised — ``FROZEN``, ``DELAYED``, ``REALTIME``, whatever arrives.
    #: ``None`` means the route said nothing, which is NOT the same as realtime:
    #: the absence of a staleness flag is not evidence of freshness. Stored per
    #: row by migration 006, because a volatility measured mid-session and one
    #: measured at the close are not the same measurement.
    market_data_status: str | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2

    @property
    def reference_price(self) -> float | None:
        """Best available spot: mid, else last. Never a fabricated number."""
        return self.mid if self.mid is not None else self.last


@dataclass(frozen=True)
class OptionExpiry:
    """One expiry inside an option month, with the month and exchange it came from."""

    expiry: str  # YYYY-MM-DD
    month: str  # IBKR's MMMYY form
    exchange: str


@dataclass(frozen=True)
class FuturesContract:
    """One rung of a futures ladder."""

    root: str
    conid: int
    expiry: str  # YYYY-MM-DD
    symbol: str | None = None
    exchange: str | None = None
    currency: str | None = None


@dataclass(frozen=True)
class AccountNav:
    """One reading of the account's net liquidation value.

    ``currency`` travels with the figure and is not decoration: every gate that
    reads NAV compares it against a premium already converted to
    ``account.base_currency``, so a value in another currency does not fail — it
    mis-sizes the sleeve by the exchange rate, silently. The caller checks it.
    ``None`` means the route reported no currency, which is a third answer and
    not a claim that it matched.
    """

    nav_base: float
    currency: str | None
    account_id: str


def _nav_currency(row: Mapping[str, Any], ledger: Mapping[str, Any] | None) -> str | None:
    """Which currency the ``BASE`` row's figure is in, or ``None`` if unidentified.

    One implementation for both routes. They decoded this separately until the
    live reading showed the label is never a currency code, and two copies of a
    rule that had just been proven wrong is two places to fix it next time.

    A label the route actually supplies wins: a route that names the code is
    telling us directly, and the rate arithmetic below is the fallback for the
    one that does not.
    """
    label = row.get("currency") or row.get("secondkey")
    code = None if label is None else str(label).upper()
    if code and code != LEDGER_BASE_KEY:
        return code
    return _base_currency_from_rates(ledger)


def _base_currency_from_rates(ledger: Mapping[str, Any] | None) -> str | None:
    """Which currency the ``BASE`` row is consolidated into, read from the rates.

    **Measured, not assumed** (`[CP ✓ 2026-09-10]`): this route's ``BASE`` row
    reports ``currency: "BASE"`` AND ``secondkey: "BASE"``, so the label the
    caller checks against ``account.base_currency`` is never a currency code and
    the mismatch refusal in :mod:`obliskiq.collect.nav` could not fire on any
    reading. A guard that cannot fire is the shape this project refuses
    everywhere else — it looks like a decision and is not.

    What identifies it instead is arithmetic the ledger already carries: every
    per-currency row states the ``exchangerate`` that converts it to base, and
    the base currency's own rate is one. Exactly one such row is an
    identification; none is a route that changed shape, and more than one is an
    ambiguity — both travel as ``None``, which is what travelled before this
    existed, so the failure mode is unchanged and only the success is new.

    ``references/ibkr-playbook.md`` owns the reading.
    """
    candidates = [
        str(key).upper()
        for key, row in (ledger or {}).items()
        if isinstance(row, Mapping)
        and str(key).upper() != LEDGER_BASE_KEY
        and safe_float(row.get("exchangerate"), None) == LEDGER_BASE_RATE
    ]
    if len(candidates) != 1:
        log.warning(
            "the ledger names no single currency at the base exchange rate; the "
            "NAV currency stays unreported",
            extra={"candidates": sorted(candidates)},
        )
        return None
    return candidates[0]


def _quote_from(conid: int, row: Mapping[str, Any]) -> Quote:
    """Build a Quote from a decoded snapshot or a recorded fixture row.

    One constructor for both routes, so a field added to the underlying set
    cannot reach one implementation and quietly miss the other.
    """
    return Quote(
        conid=int(conid),
        bid=safe_float(row.get("bid"), None),
        ask=safe_float(row.get("ask"), None),
        last=safe_float(row.get("last"), None),
        implied_vol=safe_float(row.get("implied_vol"), None),
        delta=safe_float(row.get("delta"), None),
        open_interest=safe_int(row.get("open_interest"), None),
        volume=safe_int(row.get("volume"), None),
        historical_vol_30d=safe_float(row.get("historical_vol_30d"), None),
        iv_percentile_52w=safe_float(row.get("iv_percentile_52w"), None),
        market_data_status=_status_of(row.get("market_data_status")),
        # Already normalised to a decimal by the decoder. safe_float here is a
        # pass-through, not a second conversion: converting twice would divide a
        # percent by a hundred and then by a hundred again.
        dividend_yield=safe_float(row.get("dividend_yield"), None),
    )


def _status_of(value: Any) -> str | None:
    """The reported market-data state, or ``None`` where nothing was reported.

    An empty string is nothing reported, not a state called "". Everything else
    is passed through unchanged: the set of values IBKR emits is being
    discovered, and normalising it here would discard the discovery.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@runtime_checkable
class MarketData(Protocol):
    """What a collector needs from a data provider.

    Implementations are async because the CP client is; the fixture
    implementation matches the shape so tests exercise the same code path.
    """

    @property
    def source(self) -> str:
        """The ``source`` tag written on every row this provider produced."""

    async def resolve_conid(
        self, symbol: str, *, sec_type: str = "STK", exchange: str | None = None
    ) -> int | None: ...

    async def bars(
        self,
        conid: int,
        *,
        bar: str,
        period: str,
        exchange: str | None = None,
        start_time: str | None = None,
    ) -> list[Bar]: ...

    async def quotes(self, conids: Sequence[int]) -> dict[int, Quote]: ...

    async def underlying_quotes(self, conids: Sequence[int]) -> dict[int, Quote]: ...

    async def option_months(
        self, symbol: str, *, conid: int
    ) -> tuple[tuple[str, ...], tuple[str, ...]]: ...

    async def option_strikes(
        self, conid: int, *, month: str, exchange: str
    ) -> dict[str, list[float]]: ...

    async def option_contracts(
        self, conid: int, *, month: str, exchange: str, strike: float, right: str
    ) -> list[dict[str, Any]]: ...

    async def futures_ladder(self, roots: Sequence[str]) -> dict[str, list[FuturesContract]]: ...

    async def portfolio_positions(self) -> list[dict[str, Any]]: ...

    async def account_nav(self) -> AccountNav | None: ...


class CpApiMarketData:
    """:class:`MarketData` over the Client Portal Web API."""

    def __init__(self, client: CpApiClient) -> None:
        self._client = client

    @property
    def source(self) -> str:
        return SOURCE_CP_API

    async def resolve_conid(
        self, symbol: str, *, sec_type: str = "STK", exchange: str | None = None
    ) -> int | None:
        return await self._client.resolve_conid(symbol, sec_type=sec_type, exchange=exchange)

    async def bars(
        self,
        conid: int,
        *,
        bar: str,
        period: str,
        exchange: str | None = None,
        start_time: str | None = None,
    ) -> list[Bar]:
        body = await self._client.market_history(
            conid, period=period, bar=bar, exchange=exchange, start_time=start_time
        )
        return _bars_from_body(body)

    async def quotes(self, conids: Sequence[int]) -> dict[int, Quote]:
        rows = await self._client.market_snapshot(conids)
        return {
            conid: _quote_from(conid, self._client.decode_snapshot(row))
            for conid, row in rows.items()
        }

    async def underlying_quotes(self, conids: Sequence[int]) -> dict[int, Quote]:
        """The richer field set kept once per underlying, not once per strike.

        Historical vol and the vendor IV percentile mean something for an
        underlying and nothing for an individual contract, so they are requested
        here rather than on every row of a chain.
        """
        fields = self._client.underlying_field_ids
        rows = await self._client.market_snapshot(conids, fields=fields or None)
        return {
            conid: _quote_from(conid, self._client.decode_underlying_snapshot(row))
            for conid, row in rows.items()
        }

    async def option_months(
        self, symbol: str, *, conid: int
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The OPT months and exchanges listed **for this conid**, or nothing.

        The section has to come from the same instrument the strikes will be
        requested against, and until this was keyed on the conid it did not.
        ``/iserver/secdef/search`` takes a SYMBOL and returns every listing of it
        ranked by its own relevance, and this method returned the first exact
        symbol match with options — so for a dual-listed share the months and the
        exchange list could describe the New York line while
        ``/iserver/secdef/strikes`` was asked about the Madrid one. Measured
        2026-09-10: SAN and TTE reported five and nine option months and served
        **zero strikes**, because the exchange list travelling with those months
        was ``SMART;AMEX;BATS;...`` and belonged to a listing the universe does
        not hold.

        A search that returns no row carrying ``conid`` therefore returns
        ``(), ()`` rather than some other listing's section. The caller reports
        an underlying with no option months, which is true of the conid it asked
        about — and is a failure rather than a silently foreign chain.
        """
        matches = await self._client.search_contracts(symbol)
        for match in matches:
            if match.conid == int(conid) and match.has_options:
                return match.option_months(), match.option_exchanges()
        listed = [m.conid for m in matches if m.has_options]
        log.warning(
            "no search match carries the conid the universe holds; collecting no chain",
            extra={"symbol": symbol, "conid": int(conid), "conids_with_options": listed},
        )
        return (), ()

    async def option_strikes(
        self, conid: int, *, month: str, exchange: str
    ) -> dict[str, list[float]]:
        return await self._client.option_strikes(conid, month=month, exchange=exchange)

    async def option_contracts(
        self, conid: int, *, month: str, exchange: str, strike: float, right: str
    ) -> list[dict[str, Any]]:
        return await self._client.option_contracts(
            conid, month=month, exchange=exchange, strike=strike, right=right
        )

    async def futures_ladder(self, roots: Sequence[str]) -> dict[str, list[FuturesContract]]:
        raw = await self._client.futures_ladder(roots)
        return {root: _futures_from_rows(root, rows) for root, rows in raw.items()}

    async def portfolio_positions(self) -> list[dict[str, Any]]:
        account_id = await self._client.primary_account_id()
        return await self._client.all_positions(account_id)

    async def account_nav(self) -> AccountNav | None:
        """The account's net liquidation value, from ``/portfolio/{id}/ledger``.

        The ledger is keyed by currency code with a consolidated ``BASE`` row
        beside the per-currency ones, and the values arrive as strings. ``None``
        when the base row is absent or carries no readable value — a NAV that was
        not read is not a NAV of zero, and every gate downstream is a fraction of
        this number.
        """
        account_id = await self._client.primary_account_id()
        ledger = await self._client.ledger(account_id)
        row = (ledger or {}).get(LEDGER_BASE_KEY)
        if not isinstance(row, Mapping):
            log.warning(
                "the account ledger carried no BASE row",
                extra={"keys": sorted(str(k) for k in (ledger or {}))},
            )
            return None
        nav = safe_float(row.get("netliquidationvalue"), None)
        if nav is None:
            return None
        # The BASE row names itself "BASE" rather than the currency it is in, on
        # BOTH `currency` and `secondkey` — measured, and the reason the code is
        # read from the exchange rates instead. `_nav_currency` owns the rule.
        code = _nav_currency(row, ledger)
        return AccountNav(nav_base=float(nav), currency=code, account_id=str(account_id))


class FixtureMarketData:
    """:class:`MarketData` over recorded JSON, for tests and offline dry runs.

    A missing fixture RAISES rather than returning an empty result. A collector
    that quietly reports success over nothing is the failure mode this whole
    package exists to avoid, and a test that passes because its fixture was never
    found is the same bug wearing a lab coat.
    """

    def __init__(self, root: str | Path, *, source: str = SOURCE_CP_API) -> None:
        self._root = Path(root)
        self._source = source
        self.calls: list[tuple[str, str]] = []

    @property
    def source(self) -> str:
        return self._source

    def _load(self, *parts: str) -> Any:
        relative = "/".join(parts)
        self.calls.append(("load", relative))
        path = self._root.joinpath(*parts).with_suffix(".json")
        if not path.is_file():
            raise PermanentUpstreamError(
                f"no fixture at {path}. FixtureMarketData never invents an empty "
                "result: a collector reporting success over nothing is the failure "
                "this package exists to prevent."
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_optional(self, *parts: str) -> Any:
        path = self._root.joinpath(*parts).with_suffix(".json")
        if not path.is_file():
            return None
        return self._load(*parts)

    async def resolve_conid(
        self, symbol: str, *, sec_type: str = "STK", exchange: str | None = None
    ) -> int | None:
        # Recorded conids are keyed by symbol alone; the venue tiebreak is a
        # property of a live search and has nothing to disambiguate here.
        table = self._load("conids")
        return safe_int(table.get(symbol.upper()), None)

    async def bars(
        self,
        conid: int,
        *,
        bar: str,
        period: str,
        exchange: str | None = None,
        start_time: str | None = None,
    ) -> list[Bar]:
        self.calls.append(("bars", f"{conid}:{bar}:{period}:{start_time or ''}"))
        body = self._load_optional("bars", f"{conid}_{bar}")
        if body is None:
            body = self._load("bars", str(bar))
        return _bars_from_body(body)

    async def quotes(self, conids: Sequence[int]) -> dict[int, Quote]:
        table = self._load("quotes")
        out: dict[int, Quote] = {}
        for conid in conids:
            row = table.get(str(conid))
            if row is None:
                continue
            out[int(conid)] = _quote_from(int(conid), row)
        return out

    async def underlying_quotes(self, conids: Sequence[int]) -> dict[int, Quote]:
        """The same recorded rows; the extra keys are simply absent in most."""
        return await self.quotes(conids)

    async def option_months(
        self, symbol: str, *, conid: int
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The recorded section, refused when it belongs to another conid.

        A fixture entry names the ``conid`` its OPT section came from, and a
        mismatch returns nothing — the same refusal the live route makes. Without
        that a test would pass on a section the live path rejects, which is
        exactly the shape of the defect this keying fixes.
        """
        table = self._load("option_months")
        entry = table.get(symbol.upper())
        if entry is None:
            return (), ()
        recorded = safe_int(entry.get("conid"), None)
        if recorded is not None and recorded != int(conid):
            return (), ()
        return tuple(entry.get("months") or ()), tuple(entry.get("exchanges") or ())

    async def option_strikes(
        self, conid: int, *, month: str, exchange: str
    ) -> dict[str, list[float]]:
        body = self._load("strikes", f"{conid}_{month}")
        out: dict[str, list[float]] = {}
        for side in ("call", "put"):
            values = [safe_float(v, None) for v in body.get(side, []) or []]
            out[side] = [v for v in values if v is not None]
        return out

    async def option_contracts(
        self, conid: int, *, month: str, exchange: str, strike: float, right: str
    ) -> list[dict[str, Any]]:
        body = self._load("contracts", f"{conid}_{month}_{right.upper()}")
        return [row for row in body if safe_float(row.get("strike"), None) == strike]

    async def futures_ladder(self, roots: Sequence[str]) -> dict[str, list[FuturesContract]]:
        table = self._load("futures")
        return {
            str(root): _futures_from_rows(str(root), table.get(str(root)) or []) for root in roots
        }

    async def portfolio_positions(self) -> list[dict[str, Any]]:
        return list(self._load("positions"))

    async def account_nav(self) -> AccountNav | None:
        ledger = self._load("ledger")
        row = (ledger or {}).get(LEDGER_BASE_KEY)
        if not isinstance(row, Mapping):
            return None
        nav = safe_float(row.get("netliquidationvalue"), None)
        if nav is None:
            return None
        code = _nav_currency(row, ledger)
        return AccountNav(nav_base=float(nav), currency=code, account_id=str(ledger.get("acctId")))


# ── shared decoding ──


def _bars_from_body(body: dict[str, Any]) -> list[Bar]:
    """Decode ``/iserver/marketdata/history``'s ``data`` array.

    Bars carry ``t`` (epoch ms), ``o``, ``h``, ``l``, ``c``, ``v``. A bar without
    a usable timestamp is dropped: an undated bar cannot be deduplicated, and a
    duplicate bar is worse than a missing one.
    """
    out: list[Bar] = []
    for row in (body or {}).get("data") or []:
        stamp = safe_float(row.get("t"), None)
        if stamp is None:
            continue
        out.append(
            Bar(
                ts=epoch_ms_to_stamp(stamp),
                open=safe_float(row.get("o"), None),
                high=safe_float(row.get("h"), None),
                low=safe_float(row.get("l"), None),
                close=safe_float(row.get("c"), None),
                volume=safe_float(row.get("v"), None),
            )
        )
    out.sort(key=lambda bar: bar.ts)
    return out


def _futures_from_rows(root: str, rows: Sequence[dict[str, Any]]) -> list[FuturesContract]:
    out: list[FuturesContract] = []
    for row in rows:
        conid = safe_int(row.get("conid"), None)
        expiry = parse_date(row.get("expirationDate") or row.get("expiry"))
        if conid is None or expiry is None:
            continue
        out.append(
            FuturesContract(
                root=root,
                conid=conid,
                expiry=expiry.isoformat(),
                symbol=row.get("symbol"),
                exchange=row.get("exchange") or row.get("listingExchange"),
                currency=row.get("currency"),
            )
        )
    out.sort(key=lambda contract: contract.expiry)
    return out
