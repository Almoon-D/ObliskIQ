"""marketdata.app — historical and end-of-day option chains.

WHY THIS ROUTE EXISTS
---------------------
The build was sequenced around a premise: option-chain history cannot be
retrieved retrospectively, so a day not collected is gone permanently. That is
true of IBKR and false of the market. marketdata.app serves historical
end-of-day chains carrying implied vol and full greeks.

**The two credit tiers are not the same order of magnitude, and conflating them
mis-sizes every budget in this module.** Observed live 2026-08-28: a 3-contract
request on the live/cached tier consumed 3 credits — that tier bills **per option
symbol returned**. The documented one-credit-per-thousand-symbols applies to
**historical** pulls only and is itself still unverified. So the daily run, which
collects today through the live tier, is the expensive one; the backfill, if the
historical price holds, is nearly free beside it. See
``references/marketdata-playbook.md``.

It does not replace :mod:`obliskiq.collect.chain_collector`. Three of the
fifteen tracked underlyings are European listings (ASML/AEB, SAN/BM, TTE/SBF)
which this vendor does not cover, and for those the original argument holds
exactly as written.

WHY IT IS NOT A ``MarketData`` IMPLEMENTATION
---------------------------------------------
:class:`obliskiq.collect.source.MarketData` is a nine-method protocol shaped
around IBKR conids and ``MMMYY`` months — ``option_strikes(conid, month,
exchange)``, ``option_contracts`` returning dicts keyed ``conid`` and
``maturityDate``. A ticker-and-OCC vendor fits it badly: six stubbed methods, a
synthesised conid, and a strike-by-strike walk that exists only to work around
IBKR's per-strike API. This vendor returns a whole chain in one call, filtered
server-side.

So this is a separate decoder writing the **same rows to the same tables**, and
every consumer downstream (``screen``, ``universe_census``, ``indicators``,
``smile``, ``risk``) is untouched. That is the same judgement
``references/ibkr-playbook.md`` already records about the two IBKR routes: "a
decoder written for one silently returns nothing on the other. That is correct
for their route and is recorded here so nobody 'fixes' one into the other."

WHAT THE RESPONSE LOOKS LIKE, AND WHERE IT BITES
------------------------------------------------
Verified live 2026-08-28; see ``references/marketdata-playbook.md``.

* **Columnar.** Parallel arrays — ``optionSymbol[]``, ``strike[]``, ``iv[]`` —
  the MCP shape, not the Client Portal's ``data[]`` of row objects. Zipped only
  as far as the shortest column, because a short column is a truncated response
  and pairing past it silently aligns a strike with another strike's vol.
* **HTTP 203 is success.** The vendor answers from a caching tier with 203, not
  200. ``macro/fetchers.py`` raises on anything ``!= 200``, so a base class
  copied from there would reject good data.
* **``s: "no_data"`` is not an error.** It means the symbol had no chain that
  session. Recorded as zero rows; the target is not failed.
* **Epoch SECONDS, US Eastern.** ``timeutil.epoch_ms_to_stamp`` takes
  milliseconds and yields UTC, so it is wrong twice over here — feeding it these
  values lands in January 1970. Expiries are stamped 16:00 ET, which never
  crosses midnight UTC, so the date happens to survive a UTC reading; that is a
  coincidence of the equity close and not a property to rely on, and the
  conversion is done in ET explicitly.
* **The trading date must be resolved in ET.** ``date=`` names a US session. A
  collector running at 02:00 UTC and asking for ``utc_today()`` asks for a date
  the New York session has not reached.
* **``underlyingPrice`` ships in the same response**, so spot and option quotes
  cannot disagree about the moment they describe. The delayed-spot-against-live-
  quotes failure is structurally impossible on this route.

A NOTE ON MISSING IMPLIED VOL
-----------------------------
Where the vendor serves no ``iv`` for a contract, the row is written with
``iv = NULL`` and counted. It is **not** solved from the mid price here, even
though :func:`obliskiq.quant.bsm.implied_vol` could: solving needs a risk-free
rate at the contract's own maturity and a dividend yield, neither of which this
layer holds, and inventing either would put a fabricated number in the column
the whole route exists to fill. A run whose null fraction exceeds
``option_vendor.max_null_iv_fraction`` reports **degraded** and says so.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..config import Config
from ..errors import (
    ERROR_EXCERPT_CHARS,
    NoMatchingTargets,
    ObliskError,
    PermanentUpstreamError,
    UpstreamError,
)
from ..logging_setup import get_logger
from ..store import Store
from ..util.http import RetryPolicy, request_with_retry
from ..util.timeutil import age_seconds, utc_stamp
from .retry import TargetRetryPolicy, pace, with_target_retry
from .runlog import CollectorRun
from .state import UPSTREAM_MARKETDATA_APP, active_cooldown, set_cooldown
from .universe import assert_seeded

log = get_logger("collect.option_vendor")

#: The row ``source`` tag. A genuine third route, not a reuse of 'mcp': a
#: different provider, decoder, smile and session convention. CLAUDE.md requires
#: the route recorded per row so collection can be migrated wholesale.
SOURCE_MARKETDATA_APP = "marketdata_app"

#: Chain endpoint. A module constant rather than config, matching how
#: ``macro/fetchers.py`` holds FRED's and the ECB's.
VENDOR_CHAIN_URL = "https://api.marketdata.app/v1/options/chain"

#: 200, and 203 which the caching tier answers with. Both carry a full body.
HTTP_OK = 200
HTTP_CACHED = 203
OK_STATUS = frozenset({HTTP_OK, HTTP_CACHED})

#: The one-IP block. marketdata.app permits a single source address per account
#: and answers a changed one with "Access denied... please wait 5 minutes"
#: (references/marketdata-playbook.md). It is account-wide and free —
#: ``x-api-ratelimit-consumed: 0`` — so it costs time rather than credits, and
#: it is not about the symbol that happened to discover it.
HTTP_FORBIDDEN = 403


class VendorIpBlocked(PermanentUpstreamError):
    """marketdata.app has blocked this account's address for a few minutes.

    Permanent within the run: no number of retries changes the answer, and it
    applies to every target rather than the one that hit it. Distinct from a
    plain UpstreamError so the collector can write ONE cooldown and stop, rather
    than letting each of the twelve US targets rediscover the same block.
    """


#: The ``s`` field. 'no_data' is an outcome, not a failure.
BODY_OK = "ok"
BODY_NO_DATA = "no_data"
BODY_ERROR = "error"

#: Credit accounting headers. CONSUMED is what a single response cost;
#: REMAINING is the account's running balance, and the DELTA in it is the only
#: figure that also captures attempts this layer never sees — ``request_with_retry``
#: retries inside itself and hands back just the final response, so summing
#: CONSUMED undercounts a retried call by every attempt but the last.
HEADER_CONSUMED = "x-api-ratelimit-consumed"
HEADER_REMAINING = "x-api-ratelimit-remaining"

#: The vendor's sides, and the ``chain_snapshots.right`` values they map to.
RIGHT_BY_SIDE = {"call": "C", "put": "P"}

#: Every session this vendor dates is a US one.
EASTERN = ZoneInfo("America/New_York")

#: Columns zipped into a row. A response missing any of these is truncated.
REQUIRED_COLUMNS = ("optionSymbol", "expiration", "side", "strike")

#: Excerpt length when an error body has to be reported.


class VendorError(ObliskError):
    """The vendor refused, or answered with something unusable."""


def eastern_session(moment: datetime | None = None) -> date:
    """The US trading date as New York sees it.

    ``date=`` names a US session. Deriving it from ``utc_today()`` asks for
    tomorrow's session for the several hours each day when UTC has rolled over
    and New York has not.
    """
    now = moment or datetime.now(tz=EASTERN)
    return now.astimezone(EASTERN).date()


def epoch_seconds_to_eastern_date(value: Any) -> date | None:
    """Epoch SECONDS to the calendar date in New York.

    Not milliseconds: ``timeutil.epoch_ms_to_stamp`` reads these as ms and lands
    in 1970. Not UTC either — an expiry is stamped 16:00 ET, which survives a UTC
    reading only because the equity close is early enough in the day, and that is
    a coincidence rather than a rule.
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, tz=EASTERN).date()


@dataclass(frozen=True)
class VendorContract:
    """One decoded contract. ``None`` means absent, never zero."""

    option_symbol: str
    expiry: date
    strike: float
    right: str
    bid: float | None
    ask: float | None
    last: float | None
    iv: float | None
    delta: float | None
    open_interest: int | None
    volume: int | None
    underlying_price: float | None


@dataclass(frozen=True)
class VendorChain:
    """One session's chain for one underlying, plus what the call cost."""

    symbol: str
    trade_date: date
    contracts: tuple[VendorContract, ...]
    credits: int | None
    status: str

    @property
    def underlying_price(self) -> float | None:
        for contract in self.contracts:
            if contract.underlying_price is not None:
                return contract.underlying_price
        return None

    @property
    def null_iv_count(self) -> int:
        return sum(1 for c in self.contracts if c.iv is None)

    def outside_window(self, window: tuple[date, date]) -> tuple[VendorContract, ...]:
        """Contracts the tenor filter should have excluded but did not.

        The request asks the vendor to filter server-side, which is what keeps a
        pull cheap. But a filter parameter that is silently IGNORED returns the
        whole chain at full price and looks exactly like a successful narrow
        pull, so the answer is checked against the question rather than assumed
        to match it.
        """
        low, high = window
        return tuple(c for c in self.contracts if not low <= c.expiry <= high)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _whole(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def decode_chain(
    body: dict[str, Any], *, symbol: str, trade_date: date, credits: int | None = None
) -> VendorChain:
    """Decode one columnar chain response.

    Zips only as far as the shortest present column. A truncated response is a
    real outcome on a large chain, and pairing past the truncation would align a
    strike with a different strike's implied vol — a wrong number that looks
    entirely plausible.
    """
    status = str(body.get("s") or BODY_ERROR)
    if status == BODY_NO_DATA:
        return VendorChain(symbol, trade_date, (), credits, BODY_NO_DATA)
    if status != BODY_OK:
        message = body.get("errmsg") or str(body)[:ERROR_EXCERPT_CHARS]
        raise VendorError(f"{symbol} {trade_date.isoformat()}: vendor said {status!r}: {message}")

    columns = {name: body.get(name) for name in REQUIRED_COLUMNS}
    missing = [name for name, value in columns.items() if not isinstance(value, list)]
    if missing:
        raise VendorError(
            f"{symbol} {trade_date.isoformat()}: response is missing required column(s) "
            f"{', '.join(sorted(missing))}, so no row can be keyed"
        )

    optional = ("bid", "ask", "last", "iv", "delta", "openInterest", "volume", "underlyingPrice")
    lists = dict(columns)
    for name in optional:
        value = body.get(name)
        lists[name] = value if isinstance(value, list) else []

    count = min(len(lists[name]) for name in REQUIRED_COLUMNS)
    full = max(len(lists[name]) for name in REQUIRED_COLUMNS)
    if count != full:
        log.warning(
            "vendor chain columns are ragged; zipping to the shortest",
            extra={"symbol": symbol, "shortest": count, "longest": full},
        )

    def at(name: str, index: int) -> Any:
        series = lists.get(name) or []
        return series[index] if index < len(series) else None

    contracts: list[VendorContract] = []
    for index in range(count):
        expiry = epoch_seconds_to_eastern_date(at("expiration", index))
        strike = _number(at("strike", index))
        right = RIGHT_BY_SIDE.get(str(at("side", index)).lower())
        option_symbol = at("optionSymbol", index)
        if expiry is None or strike is None or right is None or not option_symbol:
            log.warning(
                "skipping an undecodable contract",
                extra={"symbol": symbol, "index": index, "option_symbol": option_symbol},
            )
            continue
        contracts.append(
            VendorContract(
                option_symbol=str(option_symbol),
                expiry=expiry,
                strike=strike,
                right=right,
                bid=_number(at("bid", index)),
                ask=_number(at("ask", index)),
                last=_number(at("last", index)),
                iv=_number(at("iv", index)),
                delta=_number(at("delta", index)),
                open_interest=_whole(at("openInterest", index)),
                volume=_whole(at("volume", index)),
                underlying_price=_number(at("underlyingPrice", index)),
            )
        )
    return VendorChain(symbol, trade_date, tuple(contracts), credits, BODY_OK)


def atm_implied_vol(chain: VendorChain) -> tuple[float | None, str]:
    """The at-the-money implied vol, and how it was arrived at.

    ``underlying_snapshots.implied_vol`` wants one number per underlying per day
    — the series the self-computed IV percentile is built from. The vendor serves
    implied vol per contract and none for the underlying, so this is DERIVED, and
    the second element of the return says so rather than letting a computed
    figure pass for a served one.

    Both rights at the nearest strike are averaged where both are present: a
    single side at a strike carries that side's skew, and the average is the
    closer thing to the level the percentile is meant to track.

    **The NEAREST EXPIRY is selected first, and that ordering is the whole
    point.** A 90-200 DTE window holds several expiries, so averaging every
    contract at the nearest strike mixes a 90-day vol with a 200-day one — it
    returns a number off the term structure rather than a point on it. Worse, as
    expiries roll in and out of the window the mixture changes, which puts a
    sawtooth into the one series this route exists to build. Pinning the expiry
    first makes the series a consistent constant-ish maturity.
    """
    spot = chain.underlying_price
    priced = [c for c in chain.contracts if c.iv is not None and c.strike > 0]
    if spot is None or not priced:
        return None, "no_atm_available"

    expiry = min(c.expiry for c in priced)
    at_expiry = [c for c in priced if c.expiry == expiry]
    nearest = min(at_expiry, key=lambda c: abs(c.strike - spot)).strike
    at_strike = [c for c in at_expiry if c.strike == nearest]
    value = sum(c.iv for c in at_strike) / len(at_strike)  # type: ignore[misc]
    rights = "both_rights" if len({c.right for c in at_strike}) > 1 else "one_right"
    return value, f"derived_{expiry.isoformat()}_nearest_strike_{rights}"


@dataclass(frozen=True)
class VendorGates:
    """The request filters, read from ``config.yaml`` rather than hardcoded.

    The vendor filters server-side, so the mandate's own liquidity and tenor
    gates travel with the request instead of being applied after paying for rows
    that were never candidates.

    ``liquidity.require_session_volume`` is deliberately NOT sent.
    That is a SCREENING gate: filtering it at collection would remove contracts
    the grid should still see and leave the store unable to answer why one
    failed.
    """

    dte_min: int
    dte_max: int
    min_open_interest: int
    max_spread_pct: float

    @classmethod
    def from_config(cls, config: Config) -> VendorGates:
        tenor = config.section("tenor")
        liquidity = config.section("liquidity")
        return cls(
            dte_min=tenor.require_int("entry_dte_min"),
            dte_max=tenor.require_int("entry_dte_max"),
            min_open_interest=liquidity.require_int("min_open_interest"),
            max_spread_pct=liquidity.require_float("max_spread_pct"),
        )

    def as_params(self, *, session: date) -> dict[str, Any]:
        """The query for one session's chain.

        ``from``/``to`` are EXPIRATION DATE bounds on this endpoint, not a
        days-to-expiry range — ``dte`` is the days filter, and it takes a single
        value rather than a span. So the tenor window is converted to real dates
        against the session being asked for. Passing the raw DTE integers into
        ``from``/``to`` is the bug this replaced: the vendor either rejects them
        or, worse, ignores them and returns the ENTIRE chain, every expiry, which
        on the per-symbol live tier is an expensive silent failure.

        Both the parameter names and this conversion are still **unverified
        live** — the one probe that got through used ``dte``. That is why
        :meth:`VendorChain.outside_window` re-checks the answer rather than
        trusting the request.
        """
        return {
            "from": (session + timedelta(days=self.dte_min)).isoformat(),
            "to": (session + timedelta(days=self.dte_max)).isoformat(),
            "minOpenInterest": self.min_open_interest,
            "maxBidAskSpreadPct": self.max_spread_pct,
        }

    def window_for(self, session: date) -> tuple[date, date]:
        """The expiry span the request asked for, for checking what came back."""
        return (session + timedelta(days=self.dte_min), session + timedelta(days=self.dte_max))


class VendorClient:
    """Fetches chains. Owns no store and no clock beyond the ET session."""

    def __init__(self, config: Config, client: httpx.AsyncClient, token: str | None) -> None:
        self._client = client
        self._token = token
        self._policy = RetryPolicy.from_config(config)
        self._gates = VendorGates.from_config(config)
        self._assumed_credits = config.require_int("option_vendor.assumed_credits_when_unreported")
        #: Sum of the per-response CONSUMED headers this client saw.
        self._consumed = 0
        #: The account balance when this client made its first call, and the most
        #: recent balance since. Their difference is authoritative where the
        #: vendor serves it.
        self._first_remaining: int | None = None
        self._last_remaining: int | None = None

    @property
    def credits_spent(self) -> int:
        """What this client has actually cost the account.

        Two measures, and the LARGER wins. Summing ``consumed`` misses attempts
        that ``request_with_retry`` retried internally, because only the final
        response comes back here. The drop in ``remaining`` sees all of them, but
        is only available while the vendor serves that header, and would also
        pick up spend from another process sharing the key — which is a reason to
        prefer it, not to discount it: the ceiling exists to stop overspending,
        so it should believe the more pessimistic of the two.
        """
        by_balance = 0
        if self._first_remaining is not None and self._last_remaining is not None:
            by_balance = max(0, self._first_remaining - self._last_remaining)
        return max(self._consumed, by_balance)

    @property
    def has_token(self) -> bool:
        """Whether this route can run at all, checkable without calling."""
        return bool(self._token)

    async def chain(
        self, symbol: str, *, trade_date: date | None = None, dated_request: bool | None = None
    ) -> VendorChain:
        """One chain. ``trade_date`` omitted means the current ET session.

        ``dated_request`` decides whether the call carries ``date=``, which is
        what selects the historical tier over the live one. It defaults to
        "dated whenever a trade_date was given", which is the obvious reading and
        the wrong one for the newest session: that call must reach the LIVE tier
        (no ``date=``) while still stamping its rows with the run's own session
        date. Passing ``trade_date`` with ``dated_request=False`` says exactly
        that, and separating the two is what stops the collector reading the
        clock a second time — a run pinned to a past date used to stamp its
        newest session with today, silently, because the session came from
        ``eastern_session()`` rather than from the date the run was given.

        The token is checked HERE rather than in ``__init__``: a missing key must
        fail the one target that needed it, not stop a process that still has
        other collectors to run.
        """
        if not self._token:
            raise UpstreamError(
                f"MARKETDATA_API_TOKEN is unset, so the option chain for {symbol} cannot be "
                "fetched. There is no substitute for a vendor chain."
            )
        session = trade_date or eastern_session()
        dated = (trade_date is not None) if dated_request is None else dated_request
        params: dict[str, Any] = dict(self._gates.as_params(session=session))
        if dated:
            params["date"] = session.isoformat()

        response = await request_with_retry(
            self._client,
            "GET",
            f"{VENDOR_CHAIN_URL}/{symbol}/",
            policy=self._policy,
            params=params,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        credits = self._credits_of(response)
        if response.status_code == HTTP_FORBIDDEN:
            # Account-wide, not about this symbol: marketdata.app permits one IP
            # and refuses everything for five minutes when the address changes.
            # Permanent, so the target retry does not spend two more attempts on
            # an answer that cannot change, and raised as its own type so the
            # collector can record the block once instead of twelve times.
            raise VendorIpBlocked(
                f"marketdata.app refused the call for {symbol} with HTTP "
                f"{response.status_code}: {response.text[:ERROR_EXCERPT_CHARS]}"
            )
        if response.status_code not in OK_STATUS:
            raise UpstreamError(
                f"marketdata.app chain for {symbol} returned HTTP {response.status_code}: "
                f"{response.text[:ERROR_EXCERPT_CHARS]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise UpstreamError(
                f"marketdata.app chain for {symbol} returned a non-JSON body"
            ) from exc
        chain = decode_chain(body, symbol=symbol, trade_date=session, credits=credits)

        # Did the server-side tenor filter actually apply? An ignored parameter
        # returns the whole chain at full per-symbol price and is indistinguishable
        # from success by every other signal.
        stray = chain.outside_window(self._gates.window_for(session))
        if stray:
            log.warning(
                "the vendor returned expiries outside the tenor window: the from/to "
                "filter did not apply, and this pull was charged for the whole chain",
                extra={
                    "symbol": symbol,
                    "session": session.isoformat(),
                    "outside": len(stray),
                    "of": len(chain.contracts),
                    "window": [d.isoformat() for d in self._gates.window_for(session)],
                },
            )
        return chain

    def _credits_of(self, response: httpx.Response) -> int:
        """What this call cost, never ``None``.

        A missing header used to record zero, which quietly switched the run's
        credit ceiling off — a budget guard that disables itself when its input
        goes missing is worse than no guard. An unreadable header is charged at
        ``option_vendor.assumed_credits_when_unreported`` instead, so the ceiling
        keeps binding and the run stops early rather than spending unbounded.
        """
        remaining = _whole(response.headers.get(HEADER_REMAINING))
        if remaining is not None:
            if self._first_remaining is None:
                self._first_remaining = remaining
            self._last_remaining = remaining

        reported = _whole(response.headers.get(HEADER_CONSUMED))
        if reported is None:
            log.warning(
                "no credit header on the response; charging the configured assumption "
                "so the ceiling still binds",
                extra={"header": HEADER_CONSUMED, "assumed": self._assumed_credits},
            )
            reported = self._assumed_credits
        self._consumed += reported
        return reported


def trading_dates_back(end: date, sessions: int) -> list[date]:
    """Weekday dates ending at ``end``, newest first.

    Weekdays rather than a real exchange calendar on purpose: a market holiday
    answers ``no_data``, which is recorded and never re-fetched, so the calendar
    corrects itself in the fetch log at the cost of one credit-free call per
    holiday. Importing the trading calendar here would put a compute-layer
    dependency into a collector for that one saving.
    """
    out: list[date] = []
    cursor = end
    while len(out) < sessions:
        if cursor.weekday() < _SATURDAY:
            out.append(cursor)
        cursor -= timedelta(days=1)
    return out


#: Monday is 0, so Saturday is 5: anything below it is a weekday.
_SATURDAY = 5


# ═══════════════════════════════  THE COLLECTOR  ════════════════════════════


@dataclass(frozen=True)
class VendorTarget:
    """One underlying this route serves."""

    contract_id: int
    symbol: str
    exchange: str | None


def vendor_targets(
    store: Store, config: Config, *, only: Sequence[str] | None = None
) -> list[VendorTarget]:
    """Chain-collection underlyings whose listing this vendor covers.

    Split on the LISTING EXCHANGE, declared in
    ``option_vendor.serves_listing_exchanges``, rather than on currency: a
    USD-denominated instrument listed in Europe is not a US option, and a
    mapping the code inferred would be one nobody could audit. The three
    European listings fall through to ``chain_collector``, which is the only
    route that reaches them.
    """
    serves = {
        str(name).upper() for name in config.require_list("option_vendor.serves_listing_exchanges")
    }
    rows = store.execute(
        "SELECT contract_id, symbol, exchange FROM universe WHERE collect_chain = 1 ORDER BY symbol"
    ).fetchall()
    targets = [
        VendorTarget(int(r["contract_id"]), str(r["symbol"]), r["exchange"])
        for r in rows
        if r["exchange"] and str(r["exchange"]).upper() in serves
    ]
    if only:
        wanted = {s.upper() for s in only}
        targets = [t for t in targets if t.symbol.upper() in wanted]
        if not targets:
            # Same discipline as the chain and curve collectors: reporting 'ok'
            # over zero rows is indistinguishable in the log from a healthy run.
            raise NoMatchingTargets(
                f"none of the requested symbols {sorted(wanted)} is flagged for chain "
                "collection on a listing this vendor serves. The European listings "
                "collect through the IBKR route instead."
            )
    return targets


class VendorChainCollector:
    """Writes vendor chains into the same tables the IBKR route writes."""

    def __init__(self, store: Store, config: Config, client: VendorClient) -> None:
        self._store = store
        self._config = config
        self._client = client
        self._policy = TargetRetryPolicy.from_config(config)
        section = config.section("option_vendor")
        self._enabled = section.require_bool("enabled")
        self._max_credits = section.require_int("max_credits_per_run")
        self._max_null_iv = section.require_float("max_null_iv_fraction")
        self._refetch_after = section.require_float("refetch_after_seconds")
        self._ip_block_cooldown = section.require_float("ip_block_cooldown_seconds")

    @property
    def credits_spent(self) -> int:
        """Read from the CLIENT, which sees every HTTP response.

        Summing the credits carried on returned chains undercounts, because a
        call retried twice cost three times and only the last response is
        returned to this layer.
        """
        return self._client.credits_spent

    async def run(
        self,
        run: CollectorRun,
        *,
        only: Sequence[str] | None = None,
        sessions: int = 1,
        dry_run: bool = False,
        today: date | None = None,
        as_of: date | None = None,
        force: bool = False,
    ) -> int:
        """Collect ``sessions`` trading days back from ``as_of``, newest first.

        Newest first on purpose: an interrupted backfill should leave the most
        recent history in place, because that is what the screen and the census
        read. Oldest-first would spend a day's credits on data nothing yet uses.

        ``today`` and ``as_of`` are two different questions and were one until a
        live probe needed them apart:

        * ``today`` is what this run considers the CURRENT session. It is the
          tier reference — a session equal to it is requested without ``date=``
          and billed per option symbol; anything earlier carries ``date=`` and is
          billed as history. It exists so a test can pin the clock, and it should
          be left alone anywhere else.
        * ``as_of`` is the newest session to COLLECT. Defaulting it to ``today``
          is what makes the daily run ask for the live chain; setting it earlier
          is the only way to buy ONE past session without first paying for the
          current one, which is what the walk does when it starts at today.

        Conflating them meant the end of the walk was always the current session,
        so a run pinned to a past date sent an undated request, was billed on the
        live tier, and stamped the CURRENT chain with that past date.
        """
        # THREE outcomes, because there are three facts and only two of them are
        # fine. The daily sequence runs this unconditionally, so an install that
        # merges it without a token must not fail twelve targets every night --
        # each after a full retry backoff -- for a condition known before the
        # first call. That is why both branches below cost zero retries and zero
        # credits, and it is the property to preserve.
        #
        # What they must NOT do is read the same. "Told not to run" and "could
        # not run" are different states of the machine:
        if not self._enabled:
            # Told not to run. A skip, exit 0, and nothing is wrong.
            raise NoMatchingTargets(
                "option_vendor.enabled is false, so the vendor chain route is off"
            )
        if not self._client.has_token:
            # Could not run. Enabled, scheduled, and unable to collect -- so the
            # SEQUENCE is degraded and the exit code says so, while the cost stays
            # exactly what the skip cost. Reported as ONE finding rather than
            # twelve failed targets, on the precedent MacroPull set when it
            # abandons a dead source: a condition that applies to every remaining
            # target is one fact about the route, not one fact per target.
            #
            # It was a skip until a pre-deployment audit measured what that meant
            # on a fresh install: deploy/collect.env.example ships the token empty
            # and config.yaml ships the route enabled, so every night collected
            # zero US option chains and the scheduler reported success.
            self._report_no_token(run)
            return 0

        # Before the first call, and before anything else touches the network. A
        # block written by an earlier RUN is the only way this can be known: the
        # collectors are Type=oneshot, so nothing else survives between them.
        held = active_cooldown(self._store, UPSTREAM_MARKETDATA_APP)
        if held is not None:
            self._report_cooldown(run, held)
            return 0

        assert_seeded(self._store, self._config)
        current = today or eastern_session()
        end = as_of or current
        if end > current:
            # Nothing is spent finding this out. The walk would treat the future
            # date as the current session, buy today's chain undated, and file it
            # under a day that has not happened.
            raise ObliskError(
                f"as_of {end.isoformat()} is after the current session "
                f"{current.isoformat()}; a session that has not happened cannot be "
                "collected."
            )
        targets = vendor_targets(self._store, self._config, only=only)
        if not targets:
            # SKIPPED, never ok. This file argues the point itself a few lines
            # down: reporting 'ok' over zero rows is indistinguishable in the log
            # from a healthy run.
            raise NoMatchingTargets(
                "no chain-collection underlying is listed on an exchange this vendor "
                "serves. Set collect_chain in universe.yaml on a US listing, or add the "
                "exchange to option_vendor.serves_listing_exchanges."
            )

        dates = trading_dates_back(end, max(sessions, 1))
        written = 0
        for trade_date in dates:
            for target in targets:
                if self.credits_spent >= self._max_credits:
                    self._report_budget(run, trade_date)
                    return written
                try:
                    written += await self._collect_one(
                        target, trade_date, run=run, dry_run=dry_run, force=force, today=current
                    )
                except VendorIpBlocked as exc:
                    # Account-wide. Every remaining target would meet the same
                    # refusal, so the run stops here rather than confirming it
                    # eleven more times, and the block is written down so the
                    # NEXT run does not rediscover it either.
                    self._record_block(run, exc)
                    return written
                await pace(self._policy)
        return written

    def _report_no_token(self, run: CollectorRun) -> None:
        """Enabled and untokened is DEGRADED, never a clean skip.

        The distinction is the whole point. ``option_vendor.enabled: false`` is a
        decision somebody took and the run is healthy; an unset
        ``MARKETDATA_API_TOKEN`` is the route being scheduled and unable to
        answer, which is a problem whether or not anybody is watching. Both cost
        nothing, and only one of them says so.
        """
        run.succeeded(
            "no_token",
            0,
            degraded=True,
            reason=(
                "option_vendor.enabled is true and MARKETDATA_API_TOKEN is unset, so this "
                "route is scheduled and cannot collect: no US option chain was photographed "
                "tonight. Nothing was attempted, so this cost no retries and no credits. "
                "Set the token, or set option_vendor.enabled to false -- which is a "
                "decision, and reports as a skip rather than as this."
            ),
        )
        run.notes["no_token"] = True
        log.warning(
            "vendor route is enabled and has no token; collecting nothing and reporting "
            "the run degraded",
            extra={"upstream": UPSTREAM_MARKETDATA_APP},
        )

    def _report_cooldown(self, run: CollectorRun, held: dict) -> None:
        """A run inside the cooldown is DEGRADED, never ok.

        It collected nothing, and telling the timer otherwise is how a route that
        has been blocked for a week looks healthy in the log every night.
        """
        until = str(held.get("until"))
        reason = (
            f"marketdata.app is cooling down until {until} after refusing this "
            f"account's address: {held.get('reason')}. Nothing was collected and no "
            "credit was spent. A run after that time tries again on its own."
        )
        run.succeeded(f"cooldown:{until}", 0, degraded=True, reason=reason)
        run.notes["cooldown"] = dict(held)
        log.warning(
            "vendor route is in a cooldown written by an earlier run; skipping",
            extra={"upstream": UPSTREAM_MARKETDATA_APP, "until": until},
        )

    def _record_block(self, run: CollectorRun, exc: VendorIpBlocked) -> None:
        """Write the block down once, and report the run degraded."""
        until = set_cooldown(
            self._store,
            UPSTREAM_MARKETDATA_APP,
            seconds=self._ip_block_cooldown,
            reason=str(exc),
        )
        run.succeeded(
            f"ip_blocked:{until}",
            0,
            degraded=True,
            reason=(
                f"marketdata.app refused this account's address, so the run stopped and "
                f"will not call it again before {until}. A blocked call is free, so this "
                f"cost time rather than credits. The one-IP rule is in "
                f"references/marketdata-playbook.md: collection needs a host with a "
                f"stable egress address."
            ),
        )
        run.notes["cooldown"] = {"until": until, "reason": str(exc)}

    def _report_budget(self, run: CollectorRun, reached: date) -> None:
        """Stopping at the ceiling is DEGRADED, not ok.

        A backfill that collected one of five sessions and stopped is not a
        healthy run, and exiting 0 tells the timer it was.
        """
        run.succeeded(
            f"credit_ceiling:{reached.isoformat()}",
            0,
            degraded=True,
            reason=(
                f"stopped at {self.credits_spent} credits against a ceiling of "
                f"{self._max_credits}, having reached {reached.isoformat()}. What was "
                "collected is recorded in vendor_fetch_log; re-run to continue from there."
            ),
        )
        run.notes["credit_budget"] = {
            "spent": self.credits_spent,
            "ceiling": self._max_credits,
            "stopped_at": reached.isoformat(),
        }
        log.warning(
            "credit ceiling reached; stopping cleanly with the fetch log intact",
            extra={
                "spent": self.credits_spent,
                "ceiling": self._max_credits,
                "stopped_at": reached.isoformat(),
            },
        )

    async def _collect_one(
        self,
        target: VendorTarget,
        trade_date: date,
        *,
        run: CollectorRun,
        dry_run: bool,
        force: bool,
        today: date,
    ) -> int:
        label = f"{target.symbol}:{trade_date.isoformat()}"
        if not force and self._already_held(target.symbol, trade_date):
            run.succeeded(label, 0, cached=True)
            return 0

        # Compared against the run's OWN end date, not a second reading of the
        # clock: a run pinned to a past date would otherwise treat its newest
        # session as historical and take a different code path than production.
        historical = trade_date != today
        try:
            chain = await with_target_retry(
                self._policy,
                label,
                # The session is ALWAYS the run's own date; only whether the
                # request carries `date=` depends on it being historical. Passing
                # None for the newest session used to hand the stamping job back
                # to the clock, so a run pinned to a past date wrote rows dated
                # today.
                lambda: self._client.chain(
                    target.symbol, trade_date=trade_date, dated_request=historical
                ),
            )
        except VendorIpBlocked as exc:
            # The fetch log still records an error row, so the day stays
            # retryable and _already_held never mistakes a refusal for a
            # collected session. But the block is account-wide, so it is raised
            # rather than absorbed: every remaining target would meet the same
            # refusal, and run() writes the cooldown once and stops.
            self._log_fetch(
                target.symbol, trade_date, BODY_ERROR, 0, None, repr(exc), dry_run=dry_run
            )
            raise
        except Exception as exc:
            self._log_fetch(
                target.symbol, trade_date, BODY_ERROR, 0, None, repr(exc), dry_run=dry_run
            )
            run.failed(label, repr(exc))
            return 0

        if chain.status == BODY_NO_DATA or not chain.contracts:
            # A market holiday, or a name with no chain that session. Recorded so
            # the walk never pays for it twice; the weekday walk corrects itself
            # in the log rather than needing an exchange calendar.
            self._log_fetch(
                target.symbol, trade_date, BODY_NO_DATA, 0, chain.credits, None, dry_run=dry_run
            )
            run.succeeded(label, 0, no_data=True)
            return 0

        # The fetch log and the write commit TOGETHER, in one transaction. The
        # store's transactions nest by savepoint, so the two writes below compose
        # into one unit that either both lands or neither does.
        #
        # This reverses the earlier ordering, which logged the fetch first so a
        # failing write could not cause a re-buy. That traded the wrong way. The
        # window between the two commits is killable — a VM reboot, the OOM
        # killer, the unit's own TimeoutStartSec — and _already_held never
        # re-buys a settled past session logged 'ok', so a kill in that window
        # bought a day, wrote nothing, and suppressed every future attempt at it.
        # A day silently missing is worse than a day bought twice, and this is a
        # route where the day CAN be bought twice; the European chain, where it
        # cannot, does not run through here at all.
        #
        # The credit is not forgotten either way: on failure an 'error' row is
        # written afterwards carrying what the call actually cost, and an error
        # is always retryable.
        try:
            if dry_run:
                # _log_fetch returns early on a dry run and _write is skipped, so
                # the pair is trivially atomic: neither happens.
                rows = 0
            else:
                with self._store.transaction():
                    self._log_fetch(
                        target.symbol,
                        trade_date,
                        BODY_OK,
                        len(chain.contracts),
                        chain.credits,
                        None,
                        dry_run=dry_run,
                    )
                    rows = self._write(target, chain)
        except Exception as exc:
            self._log_fetch(
                target.symbol,
                trade_date,
                BODY_ERROR,
                0,
                chain.credits,
                f"write failed after a successful fetch: {exc!r}",
                dry_run=dry_run,
            )
            run.failed(label, f"write failed after a successful fetch: {exc!r}")
            return 0

        null_fraction = chain.null_iv_count / len(chain.contracts)
        degraded = null_fraction > self._max_null_iv
        run.succeeded(
            label,
            rows,
            degraded=degraded,
            reason=(
                None
                if not degraded
                else f"{chain.null_iv_count} of {len(chain.contracts)} contracts carry no implied "
                f"vol, above the {self._max_null_iv:.0%} ceiling. The rows are written with "
                "iv NULL rather than a figure solved here, which would need a rate at each "
                "contract's own maturity and a dividend yield this layer does not hold."
            ),
            contracts=len(chain.contracts),
            null_iv=chain.null_iv_count,
            credits=chain.credits,
        )
        return rows

    def _already_held(self, symbol: str, trade_date: date) -> bool:
        """Only a prior 'ok' suppresses a re-fetch.

        A blocked or errored call is logged too, so the walk can see it, but must
        stay retryable: recording a transient IP block as a collected day would
        leave a permanent hole that looks like a holiday.
        """
        row = self._store.execute(
            "SELECT status, fetched_at FROM vendor_fetch_log "
            "WHERE symbol = ? AND trade_date = ? AND vendor = ?",
            (symbol, trade_date.isoformat(), SOURCE_MARKETDATA_APP),
        ).fetchone()
        if row is None or row["status"] == BODY_ERROR:
            return False

        # A PAST session is settled: its end-of-day chain will not change, so it
        # is held forever and never re-bought. Only the CURRENT session gets the
        # refetch window, because a chain pulled before the close is a mid-session
        # partial that would otherwise be frozen as that day's EOD row with no
        # remedy short of --force, which re-buys everything.
        if trade_date < eastern_session() and row["status"] == BODY_OK:
            return True
        age = age_seconds(row["fetched_at"])
        return age is not None and age < self._refetch_after

    def _log_fetch(
        self,
        symbol: str,
        trade_date: date,
        status: str,
        contracts: int,
        credits: int | None,
        detail: str | None,
        *,
        dry_run: bool,
    ) -> None:
        if dry_run:
            return
        with self._store.transaction() as connection:
            connection.execute(
                "INSERT INTO vendor_fetch_log (symbol, trade_date, vendor, fetched_at, "
                "status, contracts, credits, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (symbol, trade_date, vendor) DO UPDATE SET "
                "fetched_at = excluded.fetched_at, status = excluded.status, "
                "contracts = excluded.contracts, credits = excluded.credits, "
                "detail = excluded.detail",
                (
                    symbol,
                    trade_date.isoformat(),
                    SOURCE_MARKETDATA_APP,
                    utc_stamp(),
                    status,
                    contracts,
                    credits,
                    detail,
                ),
            )

    def _write(self, target: VendorTarget, chain: VendorChain) -> int:
        session = chain.trade_date.isoformat()
        stamp = utc_stamp()
        rows = [
            (
                session,
                target.contract_id,
                c.expiry.isoformat(),
                c.strike,
                c.right,
                None,  # contract_id: the vendor names contracts by OCC symbol, not conid
                c.bid,
                c.ask,
                c.last,
                c.iv,
                c.delta,
                c.open_interest,
                c.volume,
                SOURCE_MARKETDATA_APP,
                stamp,
                c.option_symbol,
            )
            for c in chain.contracts
        ]
        with self._store.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO chain_snapshots (
                    snapshot_date, underlying_id, expiry, strike, right, contract_id,
                    bid, ask, last, iv, delta, open_interest, volume, source,
                    collected_at, option_symbol)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (snapshot_date, underlying_id, expiry, strike, right)
                DO UPDATE SET
                    contract_id = excluded.contract_id, bid = excluded.bid,
                    ask = excluded.ask, last = excluded.last, iv = excluded.iv,
                    delta = excluded.delta, open_interest = excluded.open_interest,
                    volume = excluded.volume, source = excluded.source,
                    collected_at = excluded.collected_at,
                    option_symbol = excluded.option_symbol
                """,
                rows,
            )
            atm, provenance = atm_implied_vol(chain)
            connection.execute(
                """
                INSERT INTO underlying_snapshots (
                    snapshot_date, contract_id, last, implied_vol, source, collected_at,
                    implied_vol_source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (snapshot_date, contract_id) DO UPDATE SET
                    last = excluded.last, implied_vol = excluded.implied_vol,
                    collected_at = excluded.collected_at,
                    implied_vol_source = excluded.implied_vol_source
                """,
                (
                    session,
                    target.contract_id,
                    chain.underlying_price,
                    atm,
                    SOURCE_MARKETDATA_APP,
                    stamp,
                    provenance,
                ),
            )
        return len(rows)
