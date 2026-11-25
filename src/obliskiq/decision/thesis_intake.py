"""The thesis book's intake: one door for an idea from any source (MASTER_PLAN §5.5).

:meth:`ThesisIntake.submit` takes a draft from the owner's command or, later, the analyst. It
opens no brokerage session, so it is safe beside a running service:

1. the draft's own words are screened (``engines/thesis/gate.py``): a missing target, horizon or
   invalidation refuses it, with the reason;
2. its **instrument** is found. A universe symbol must be a tradable candidate, pinned and
   described, on a venue with a calendar. A share outside it is named by its stock candidate's
   key (``data/candidates.py``): a company an issuer's holdings file lists as an equity line, with
   a listing on a configured venue, and the ISIN the tax lots are kept by; or by its ticker on the
   SEC's latest listings (``data/stocks``), its identity the SEC's (CIK, ticker, exchange) and its
   ISIN given when known (DECISIONS D195). A fund, ETF or ETC enters through a reviewed universe
   entry, because its eligibility is judged on declared attributes;
3. a bearish view held long is accepted only on an instrument of a defensive core role; an idea
   that is an option or nothing (it says ``option``, or its levels describe a fall) needs an
   instrument whose option chain is collected, which need not be one the account may buy: the
   option is judged at the order, not its underlying;
4. the thesis is stored as a candidate. The service's next cycle resolves a stock candidate
   through IBKR's contract search and pins it, then judges the thesis at the gate
   (``decision/rebalance_thesis.py``).

A refused draft is not stored: the caller is told every finding.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import date, datetime, timedelta

from ..config.schema import ObliskConfig
from ..data.candidates import Candidate, Quoted, candidate_cells
from ..domain.enums import AssetClass
from ..domain.orders import TERMINAL_STATES
from ..domain.stocks import StockListing
from ..domain.tax import isin_check_digit_holds
from ..domain.thesis import (
    UNSTORED,
    Expression,
    Thesis,
    ThesisDraft,
    ThesisInstrument,
    ThesisSource,
    ThesisState,
    sec_key,
)
from ..engines.thesis.gate import Finding, Reason, screen, view_findings
from ..store import orders as repo
from ..store import stocks as stocks_repo
from ..store import theses as thesis_repo
from ..store import thesis_options as link_repo
from ..store import universe as universe_repo
from ..store.db import Store
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now

#: What a scanner's row is credited as (``data/candidates.py``): it names no asset class.
SCANNER_SOURCE = "scanner "


def find_candidate(
    settings: ObliskConfig, store: Store, key: str, *, as_of: date
) -> tuple[Candidate, str | None] | None:
    """The stock candidate ``key`` as of ``as_of`` and the grid sector it is credited to
    (``None``: only the broad cell lists it)."""
    cells = candidate_cells(
        settings,
        universe_repo.candidate_holdings(store, as_of=as_of),
        universe_repo.scanner_rows(store, as_of=as_of),
    )
    broad = settings.universe.grid.broad_sector
    found: Candidate | None = None
    sector: str | None = None
    for cell in cells:
        match = next((c for c in cell.candidates if c.key == key), None)
        if match is None:
            continue
        found = match
        if cell.sector != broad:
            sector = cell.sector
    return None if found is None else (found, sector)


def find_listing(
    store: Store, *, as_of: date, ticker: str | None = None, cik: int | None = None
) -> StockListing | None:
    """The name on the SEC's latest listings as of ``as_of``, by its ticker or its CIK (the first
    by ticker when a company lists several)."""
    snapshot = stocks_repo.snapshot_as_of(store, as_of=as_of)
    if snapshot is None:
        return None
    wanted = None if ticker is None else ticker.strip().upper()
    return next(
        (
            listing
            for listing in stocks_repo.listed(store, snapshot)
            if (wanted is None or listing.ticker == wanted) and (cik is None or listing.cik == cik)
        ),
        None,
    )


def listing_sector(settings: ObliskConfig, store: Store, cik: int, *, as_of: date) -> str | None:
    """The SIC division a listed company is counted under by the book's sector cap."""
    division = settings.sources.stocks.screen.division(
        stocks_repo.company_sics(store, as_of=as_of).get(cik)
    )
    return None if division is None else f"SIC {division.name}"


def listing_country(settings: ObliskConfig, store: Store, cik: int, *, as_of: date) -> str | None:
    """A listed company's issuer country: the home country where its submissions state a home
    incorporation, else ``None`` (unstated)."""
    config = settings.sources.stocks.listings
    code = stocks_repo.incorporation(store, cik, as_of=as_of)
    return config.home_country if code in config.home_incorporations else None


def listing_candidate(settings: ObliskConfig, listing: StockListing) -> Candidate | None:
    """A listed stock as IBKR's contract search is asked for it: its ticker on the listing
    exchange its SEC exchange maps to, in the listings' currency; ``None`` where none is mapped."""
    config = settings.sources.stocks.listings
    exchange = config.ibkr_exchanges.get(listing.exchange)
    if exchange is None:
        return None
    return Candidate(
        key=sec_key(listing.cik),
        name=listing.name,
        listings=(Quoted(listing.ticker, exchange, config.currency),),
        country=None,
        weight=None,
        sources=("sec",),
        since=None,
    )


def stock_candidate(
    settings: ObliskConfig, store: Store, instrument: ThesisInstrument, *, as_of: date
) -> Candidate | str:
    """The share a thesis names outside the universe file, as the contract search is asked for
    it, or why it cannot be asked any more."""
    key = instrument.candidate_key or ""
    cik = instrument.sec_cik
    if cik is None:
        found = find_candidate(settings, store, key, as_of=as_of)
        return f"no stock candidate {key!r} any more" if found is None else found[0]
    listing = find_listing(store, as_of=as_of, ticker=instrument.symbol, cik=cik)
    if listing is None:
        return f"{instrument.symbol} (CIK {cik}) is not on the SEC's latest listings any more"
    candidate = listing_candidate(settings, listing)
    if candidate is None:
        return f"{listing.ticker}'s exchange {listing.exchange} maps to no IBKR listing exchange"
    return candidate


def _refused(code: Reason, reason: str) -> list[Finding]:
    return [Finding(code, reason, permanent=True)]


class ThesisIntake:
    """Screens a draft, finds its instrument and stores the thesis."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        worker: StoreWorker,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._worker = worker
        self._now = now

    def _from_universe(
        self, symbol: str, expression: Expression, falls: bool
    ) -> ThesisInstrument | list[Finding]:
        settings = self._settings
        declared = settings.universe.instrument(symbol)
        if declared is None:
            return _refused(
                Reason.INSTRUMENT_UNKNOWN,
                f"{symbol} is not in config/universe.yaml; a share outside it is named by its "
                "stock candidate (--candidate), a fund through a reviewed universe entry",
            )
        option_only = falls or expression is Expression.OPTION
        if option_only and not declared.collects_chain:
            why = declared.options_unavailable_reason or "no option chain is collected on it"
            return _refused(
                Reason.NO_OPTIONS,
                f"{declared.symbol} cannot be held as an option, so nothing is held: {why}",
            )
        # An option's underlying need not be one the account may buy; an idea that may fall
        # back on its instrument (auto) needs one it may, unless its chain is collected.
        optionable = declared.collects_chain and expression is not Expression.SHARES
        if declared.conid is None or declared.description is None:
            return _refused(
                Reason.NOT_TRADABLE,
                f"{declared.symbol} cannot be ordered: it is not pinned and described yet",
            )
        if not declared.tradable and not optionable:
            why = declared.data_only_reason or "it is not a tradable candidate"
            return _refused(Reason.NOT_TRADABLE, f"{declared.symbol} cannot be ordered: {why}")
        if declared.listing_exchange not in settings.ibkr_exchanges:
            return _refused(
                Reason.NO_CALENDAR,
                f"{declared.symbol}'s listing exchange {declared.listing_exchange} maps to no "
                "venue with a calendar",
            )
        exposures = declared.exposures
        grid = exposures.grid
        broad = settings.universe.grid.broad_sector
        defensive = set(settings.thesis.defensive_roles)
        return ThesisInstrument(
            symbol=declared.symbol,
            candidate_key=None,
            conid=declared.conid,
            listing_exchange=declared.listing_exchange,
            currency=declared.currency,
            asset_class=declared.asset_class,
            description=declared.description,
            sector=None if grid is None or grid.sector == broad else grid.sector,
            country=exposures.country,
            defensive=bool(defensive & set(exposures.core_roles)),
            isin=declared.attributes.isin,
        )

    def _from_candidate(
        self, store: Store, key: str, isin: str, today: date
    ) -> ThesisInstrument | list[Finding]:
        found = find_candidate(self._settings, store, key, as_of=today)
        if found is None:
            return _refused(
                Reason.INSTRUMENT_UNKNOWN,
                f"no stock candidate {key!r} as of {today.isoformat()} (obliskiq collect "
                "candidates refreshes them)",
            )
        candidate, sector = found
        if all(source.startswith(SCANNER_SOURCE) for source in candidate.sources):
            return _refused(
                Reason.INSTRUMENT_UNKNOWN,
                f"only a market scanner shows {candidate.name}: no holdings file says it is a "
                "share, and a fund is bought only through a reviewed universe entry",
            )
        listings = candidate.venue_listings()
        if not listings:
            return _refused(
                Reason.NO_CALENDAR,
                f"{candidate.name} has no listing on a venue with a calendar and a known currency",
            )
        return ThesisInstrument(
            symbol=listings[0].ticker,
            candidate_key=candidate.key,
            conid=None,
            listing_exchange=None,
            currency=None,
            asset_class=AssetClass.STOCK,
            description=candidate.name,
            sector=sector,
            country=candidate.country,
            defensive=False,
            isin=isin,
        )

    def _from_listing(
        self, store: Store, ticker: str, isin: str | None, today: date
    ) -> ThesisInstrument | list[Finding]:
        listing = find_listing(store, as_of=today, ticker=ticker)
        if listing is None:
            return _refused(
                Reason.INSTRUMENT_UNKNOWN,
                f"{ticker.upper()} is not on the SEC's listings as of {today.isoformat()}",
            )
        if listing_candidate(self._settings, listing) is None:
            return _refused(
                Reason.NO_CALENDAR,
                f"{listing.ticker}'s exchange {listing.exchange} maps to no IBKR listing exchange",
            )
        return ThesisInstrument(
            symbol=listing.ticker,
            candidate_key=sec_key(listing.cik),
            conid=None,
            listing_exchange=None,
            currency=None,
            asset_class=AssetClass.STOCK,
            description=listing.name,
            sector=listing_sector(self._settings, store, listing.cik, as_of=today),
            country=listing_country(self._settings, store, listing.cik, as_of=today),
            defensive=False,
            isin=isin,
        )

    def _submit(self, store: Store, draft: ThesisDraft, actor: str) -> Thesis | list[Finding]:
        at = self._now()
        today = at.date()
        findings = screen(draft, today=today)
        if findings:
            return findings
        # The screen refused a draft without a target or an invalidation.
        falls = (draft.target or 0) < (draft.invalidation or 0)
        if draft.symbol is not None:
            instrument = self._from_universe(draft.symbol, draft.expression, falls)
        elif falls or draft.expression is Expression.OPTION:
            return _refused(
                Reason.NO_OPTIONS,
                "a share outside the universe file has no collected option chain, so it cannot "
                "be held as an option and nothing is held",
            )
        elif draft.listing is not None:
            instrument = self._from_listing(store, draft.listing, draft.isin, today)
        else:
            instrument = self._from_candidate(store, draft.candidate or "", draft.isin or "", today)
        if isinstance(instrument, list):
            return instrument
        findings = view_findings(draft.view, instrument, falls=falls)
        if findings:
            return findings
        horizon, expectation = draft.horizon, draft.expectation
        target, invalidation = draft.target, draft.invalidation
        # The screen refused a draft without any of them.
        assert horizon is not None
        assert expectation is not None
        assert target is not None
        assert invalidation is not None
        window = timedelta(days=self._settings.thesis.entry_window_days)
        thesis = Thesis(
            thesis_id=UNSTORED,
            source=draft.source,
            conviction=draft.conviction,
            view=draft.view,
            expression=draft.expression,
            instrument=instrument,
            expectation=expectation,
            target=target,
            invalidation=invalidation,
            horizon=horizon,
            entry_max=draft.entry_max,
            enter_by=draft.enter_by or min(today + window, horizon),
            state=ThesisState.CANDIDATE,
            created_at=at,
        )
        return thesis_repo.insert_thesis(store, thesis, actor=actor)

    async def submit(self, draft: ThesisDraft, *, actor: str) -> Thesis | list[Finding]:
        """The stored candidate, or every reason the draft is refused."""
        return await self._worker.run(self._submit, draft, actor)

    def _give_isin(self, store: Store, thesis_id: int, isin: str, actor: str) -> Thesis | str:
        thesis = thesis_repo.get_thesis(store, thesis_id)
        if thesis is None:
            return f"no thesis {thesis_id}"
        if thesis.instrument.sec_cik is None:
            return f"{thesis.ref} names no listed stock: its instrument carries its ISIN already"
        if thesis.state is not ThesisState.CANDIDATE:
            return (
                f"{thesis.ref} is {thesis.state.value}: an ISIN is given while the idea is a "
                "candidate (its identity is fixed at approval)"
            )
        return thesis_repo.give_isin(store, thesis_id, isin, at=self._now(), actor=actor)

    async def give_isin(self, thesis_id: int, isin: str, *, actor: str) -> Thesis | str:
        """The thesis with the ISIN of its listed stock, or why it cannot take one. The ISIN is
        checked for its form and check digit (``domain/tax.py``)."""
        checked = ThesisDraft(source=ThesisSource.OWNER, isin=isin).isin
        assert checked is not None  # the draft validated it
        if not isin_check_digit_holds(checked):
            return f"{checked} is not an ISIN: its check digit does not hold"
        return await self._worker.run(self._give_isin, thesis_id, checked, actor)

    def _withdraw(self, store: Store, thesis_id: int, actor: str) -> Thesis | str:
        thesis = thesis_repo.get_thesis(store, thesis_id)
        if thesis is None:
            return f"no thesis {thesis_id}"
        if thesis.state not in thesis_repo.TRANSITIONS or (
            ThesisState.WITHDRAWN not in thesis_repo.TRANSITIONS[thesis.state]
        ):
            return (
                f"{thesis.ref} is {thesis.state.value}: an idea is withdrawn before its first "
                "fill; a position leaves by the thesis's own exits"
            )
        conid = thesis.instrument.conid
        chosen = link_repo.get(store, thesis_id)
        if chosen is not None:
            # A thesis approved as an option trades the option, not its instrument.
            conid = chosen.option_conid
        working = [
            stored.intent.intent_id
            for stored in (repo.intents_for_conid(store, conid) if conid is not None else [])
            if stored.intent.thesis_id == thesis.ref and stored.intent.state not in TERMINAL_STATES
        ]
        if working:
            return (
                f"{thesis.ref}'s entry order is working (intent {working[0]}): withdraw it once "
                "that order has ended"
            )
        at = self._now()
        if (
            conid is not None
            and thesis.approved_at is not None
            and repo.fills_for_conid(store, conid, since=thesis.approved_at, as_of=at)
        ):
            return f"{thesis.ref}'s entry has filled: it leaves by the thesis's own exits"
        return thesis_repo.move(
            store,
            thesis_id,
            ThesisState.WITHDRAWN,
            at=at,
            actor=actor,
            detail="withdrawn by its source",
        )

    async def withdraw(self, thesis_id: int, *, actor: str) -> Thesis | str:
        """The withdrawn thesis, or why it cannot be withdrawn."""
        return await self._worker.run(self._withdraw, thesis_id, actor)
