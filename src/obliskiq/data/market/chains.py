"""Daily option-chain snapshots of every option underlying: the surface stored forward.

Ported from lite's ``collect/chain_collector.py``. Option price history is **not retrievable
from IBKR afterwards** (no as-of endpoint for chains; an option contract's history answers "no
data" even at 49,114 open interest, lite 2026-08), so daily self-collection is the only route to
per-strike implied-volatility history (H8, DECISIONS D71): a day not collected is gone.

What is collected: only expiries inside the tenor window (``tenor.entry_dte_min``..``max``), at
most ``max_expiries_per_underlying``, and only strikes inside each expiry's volatility-scaled band
(``strikes.py``), capped per expiry (lower when the sigma came from a fallback rung). Selection
keys off the ``option_underlying`` role, never eligibility: an EU retail account cannot buy SPY
for want of a KID but may trade options on it.

**The call shape.** The option months and the venues come from the contract search, read against
the conid the universe pins (another listing of the ticker having options does not count). Per
month, one ``/iserver/secdef/info`` call at the strike nearest spot names every expiry; each
wanted strike is then resolved once for the whole month and a row is kept only where the strike
falls inside its own expiry's band. **A declared ``option_exchange`` is sent verbatim**, even
when the search's venue list omits it: that list describes whichever listing the search matched
(a European share came back carrying ``SMART;AMEX;...``, true of its New York line and false of
it), and trusting it collected nothing. Every row records the venue it was asked on beside the
route, and the contract's multiplier as IBKR's details state it.

**Fail loud.** Zero rows is a failure, never a quiet success (SAN and TTE reported ok over no
chain on 2026-09-10). The underlying's snapshot and its chain rows commit in one transaction, so
a snapshot never stands without the chain it was taken for.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import functools
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime

from ...calendars.service import Calendars
from ...config.schema import ObliskConfig
from ...domain.enums import OptionRight
from ...domain.market import ChainRow, Snapshot
from ...errors import ObliskError
from ...ibkr.client import MarketData, Secdef
from ...ibkr.models import ContractInfo, ContractMatch, Quote
from ...logging_setup import get_logger
from ...store import repos
from ...store.db import Store
from ...store.worker import StoreWorker
from .plumbing import (
    MARKET_SOURCE,
    Collection,
    Pacer,
    PermanentCollectionError,
    failure_text,
)
from .snapshots import snapshot_of
from .strikes import (
    StrikeBounds,
    TenorWindow,
    diffusion_tau_years,
    nearest_strike,
    select_strikes,
    strike_bounds,
)
from .targets import MarketTarget, venue_session
from .vol import Sigma, SigmaLadder

log = get_logger("data.market.chains")

STEP = "chains"
OPTION_SEC_TYPE = "OPT"
#: IBKR's maturity form, YYYYMMDD.
_MATURITY_LENGTH = len("YYYYMMDD")


class ChainCollectionError(ObliskError):
    """A chain that could not be collected today (it may on a retry)."""


@dataclass(frozen=True, slots=True)
class Leg:
    """One resolved option contract of the walk."""

    expiry: date
    strike: float
    right: OptionRight
    info: ContractInfo


def maturity(text: str | None) -> date | None:
    """IBKR's ``YYYYMMDD`` maturity, or ``None`` when it is not one."""
    if text is None or len(text) != _MATURITY_LENGTH or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def option_exchange(target: MarketTarget, exchanges: tuple[str, ...]) -> str:
    """The venue the option requests send: the declared one outright; else the listing venue
    when the search lists it, else the first venue the search lists."""
    if target.option_exchange:
        if exchanges and target.option_exchange not in exchanges:
            log.warning(
                "the declared option exchange is not in the search's list; sending it anyway",
                extra={"symbol": target.symbol, "declared": target.option_exchange},
            )
        return target.option_exchange
    if target.listing_exchange in exchanges:
        return target.listing_exchange
    return exchanges[0] if exchanges else target.listing_exchange


def write_chain(store: Store, snapshot: Snapshot | None, rows: list[ChainRow]) -> int:
    """The underlying's snapshot and its chain rows, together or not at all."""
    with store.transaction():
        if snapshot is not None:
            repos.upsert_snapshots(store, [snapshot])
        return repos.upsert_chain_rows(store, rows)


class ChainCollector:
    """Walks the option underlyings and writes one session of chain rows each."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        secdef: Secdef,
        marketdata: MarketData,
        worker: StoreWorker,
        pacer: Pacer,
        calendars: Calendars,
        now: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._chain = settings.collectors.chain
        self._vol = settings.collectors.vol
        self._basis = settings.numerics.bsm.day_count_basis
        self._secdef = secdef
        self._marketdata = marketdata
        self._worker = worker
        self._pacer = pacer
        self._calendars = calendars
        self._now = now
        self._window = TenorWindow(settings.tenor)
        self._ladder = SigmaLadder(settings.collectors)

    async def run(self, targets: Iterable[MarketTarget], *, dry_run: bool = False) -> Collection:
        collection = Collection(STEP)
        for target in targets:
            if not target.collect_chain:
                continue
            try:
                await self._pacer.retry(
                    target.symbol, functools.partial(self._one, target, collection, dry_run=dry_run)
                )
            except Exception as exc:  # one underlying never stops the step
                collection.failed(target.symbol, failure_text(exc))
            await self._pacer.pace()
        if not collection.outcomes:
            collection.empty_reason = "no option underlying of the universe is on this venue"
        return collection

    async def _one(self, target: MarketTarget, collection: Collection, *, dry_run: bool) -> None:
        session = venue_session(self._calendars, target.venue, self._now())
        quotes = await self._marketdata.snapshot([target.conid], underlying=True)
        quote = quotes.get(target.conid)
        await self._pacer.pace()
        bars = await self._worker.run(
            repos.bar_series, target.conid, as_of=session, limit=self._ladder.closes_needed
        )
        spot = self._spot(quote, bars[-1].close if bars else None)
        if spot is None:
            raise PermanentCollectionError(
                f"no spot for {target.symbol}: the snapshot carried no price and no daily bar "
                "is held; a strike band cannot be centred on a price never observed"
            )
        sigma = self._ladder.resolve(
            [bar.close for bar in bars],
            vendor_iv=quote.implied_vol if quote is not None else None,
            newest_close=bars[-1].session_date if bars else None,
            session=session,
        )
        degraded = self._degraded(target, sigma, session)
        months, exchanges = await self._months(target)
        exchange = option_exchange(target, exchanges)
        in_window = self._window.months(months, session)
        if not in_window:
            raise PermanentCollectionError(
                f"{target.symbol} lists no option month in the {self._window.dte_min}-"
                f"{self._window.dte_max} day window (months {list(months)})"
            )
        cap = (
            self._chain.max_strikes_per_expiry_on_fallback
            if sigma.is_fallback
            else self._chain.max_strikes_per_expiry
        )
        legs: list[Leg] = []
        for month in in_window:
            remaining = self._chain.max_expiries_per_underlying - len({leg.expiry for leg in legs})
            if remaining <= 0:
                break
            legs += await self._month(target, month, exchange, spot, sigma, session, cap, remaining)
        if not legs:
            raise ChainCollectionError(
                f"{target.symbol} (conid {target.conid}) resolved no option contract on "
                f"{exchange} across months {in_window}; the search reported venues "
                f"{list(exchanges)}. Either the venue is wrong for this conid or the band "
                "excluded every listed strike"
            )
        option_quotes = await self._marketdata.snapshot([leg.info.conid for leg in legs])
        at = self._now()
        rows = [
            self._row(target, leg, option_quotes.get(leg.info.conid), sigma, exchange, session, at)
            for leg in legs
        ]
        if not dry_run:
            # An underlying whose snapshot carried no price (an index, often) keeps no snapshot
            # row: the bar close its band was centred on is not a quote.
            snapshot = (
                snapshot_of(quote, session, at) if quote is not None and quote.priced else None
            )
            await self._worker.run(write_chain, snapshot, rows)
        collection.succeeded(
            target.symbol,
            len(rows),
            degraded=degraded,
            reason=f"the band was sized from {sigma.source}"
            + (" on stale bars" if sigma.is_stale else "")
            if degraded
            else "",
            sigma=round(sigma.value, 6),
            sigma_source=sigma.source,
            spot=spot,
            exchange=exchange,
            expiries=sorted({str(leg.expiry) for leg in legs}),
        )

    @staticmethod
    def _spot(quote: Quote | None, close: float | None) -> float | None:
        """The mid of a two-sided book, else the last, else the newest bar's close; an index's
        snapshot is often empty, so the bar is not a nicety. Never an invented price."""
        if quote is not None:
            for price in (quote.mid, quote.last):
                if price is not None and price > 0:
                    return price
        return close if close is not None and close > 0 else None

    def _degraded(self, target: MarketTarget, sigma: Sigma, session: date) -> bool:
        """A stale realised rung, or a fallback on an instrument not listed recently."""
        if sigma.is_stale:
            return True
        if not sigma.is_fallback:
            return False
        listed = target.inception
        return listed is None or (session - listed).days > self._chain.new_listing_grace_days

    async def _months(self, target: MarketTarget) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The option months and venues the search lists for the pinned conid itself."""
        matches: list[ContractMatch] = await self._secdef.search(target.symbol)
        await self._pacer.pace()
        match = next((m for m in matches if m.conid == target.conid), None)
        section = match.section(OPTION_SEC_TYPE) if match is not None else None
        if section is None or not section.months:
            raise PermanentCollectionError(
                f"{target.symbol} (conid {target.conid}) reports no option months; the section "
                "is read against the pinned conid, not another listing of the ticker"
            )
        return section.months, section.exchanges

    async def _month(
        self,
        target: MarketTarget,
        month: str,
        exchange: str,
        spot: float,
        sigma: Sigma,
        session: date,
        cap: int,
        max_expiries: int,
    ) -> list[Leg]:
        ladder = await self._secdef.strikes(target.conid, month=month, exchange=exchange)
        await self._pacer.pace()
        anchor = nearest_strike(ladder.call or ladder.put, spot)
        if anchor is None:
            log.warning("no strikes listed", extra={"symbol": target.symbol, "month": month})
            return []
        named = await self._secdef.option_contracts(
            target.conid, month=month, exchange=exchange, strike=anchor, right=OptionRight.CALL
        )
        await self._pacer.pace()
        found = [d for info in named if (d := maturity(info.maturity_date)) is not None]
        expiries = self._window.expiries(found, session)[:max_expiries]
        wanted: dict[tuple[date, OptionRight], set[float]] = {}
        for expiry in expiries:
            bounds = self._bounds(expiry, session, spot, sigma)
            for right, side in ((OptionRight.CALL, ladder.call), (OptionRight.PUT, ladder.put)):
                wanted[(expiry, right)] = set(select_strikes(side, bounds, cap=cap))
        legs: list[Leg] = []
        for right in (OptionRight.CALL, OptionRight.PUT):
            union = sorted({s for (_, r), strikes in wanted.items() if r is right for s in strikes})
            for strike in union:
                contracts = await self._secdef.option_contracts(
                    target.conid, month=month, exchange=exchange, strike=strike, right=right
                )
                await self._pacer.pace()
                for info in contracts:
                    expires = maturity(info.maturity_date)
                    # Keyed on the strike ASKED for: an echo that round-trips differently must
                    # not drop the row silently.
                    if expires is None or strike not in wanted.get((expires, right), ()):
                        continue
                    legs.append(Leg(expires, strike, right, info))
        return legs

    def _bounds(self, expiry: date, session: date, spot: float, sigma: Sigma) -> StrikeBounds:
        tau = diffusion_tau_years(
            (expiry - session).days,
            trading_days_per_year=self._vol.trading_days_per_year,
            calendar_days_per_year=self._basis,
        )
        return strike_bounds(
            spot=spot, sigma=sigma.value, tau_years=tau, k=self._chain.strike_bounds_sigma
        )

    @staticmethod
    def _row(
        target: MarketTarget,
        leg: Leg,
        quote: Quote | None,
        sigma: Sigma,
        exchange: str,
        session: date,
        at: datetime,
    ) -> ChainRow:
        multiplier = leg.info.multiplier
        delta = quote.delta if quote is not None else None
        if delta is not None and not (math.isfinite(delta) and -1.0 <= delta <= 1.0):
            delta = None  # not a delta: unreported rather than a refused row
        return ChainRow(
            snapshot_date=session,
            underlying_conid=target.conid,
            expiry=leg.expiry,
            strike=leg.strike,
            right=leg.right,
            option_conid=leg.info.conid,
            option_symbol=leg.info.local_symbol,
            option_exchange=exchange,
            bid=quote.bid if quote is not None else None,
            ask=quote.ask if quote is not None else None,
            last=quote.last if quote is not None else None,
            iv=quote.implied_vol if quote is not None else None,
            delta=delta,
            open_interest=quote.open_interest if quote is not None else None,
            volume=quote.volume if quote is not None else None,
            sigma_used=sigma.value,
            sigma_source=sigma.source,
            multiplier=multiplier if multiplier is not None and multiplier > 0 else None,
            source=MARKET_SOURCE,
            collected_at=at,
            market_data_status=quote.market_data_status if quote is not None else None,
            market_data_raw=quote.status_flag if quote is not None else None,
            quote_delay_seconds=quote.delay_seconds if quote is not None else None,
        )
