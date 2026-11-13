"""The thesis book's intake: one door for an idea from any source (MASTER_PLAN §5.5).

:meth:`ThesisIntake.submit` takes a draft from the owner's command or, later, the analyst. It
opens no brokerage session, so it is safe beside a running service:

1. the draft's own words are screened (``engines/thesis/gate.py``): a missing target, horizon or
   invalidation refuses it, with the reason;
2. its **instrument** is found. A universe symbol must be a tradable candidate, pinned and
   described, on a venue with a calendar. Anything else is named by its stock candidate's key
   (``data/candidates.py``): a company an issuer's holdings file lists as an equity line, with a
   listing on a configured venue, and the ISIN the tax lots are kept by. A fund, ETF or ETC enters
   through a reviewed universe entry, because its eligibility is judged on declared attributes;
3. a bearish view is accepted only on an instrument of a defensive core role;
4. the thesis is stored as a candidate. The service's next cycle resolves a stock candidate
   through IBKR's contract search and pins it, then judges the thesis at the gate
   (``decision/rebalance_thesis.py``).

A refused draft is not stored: the caller is told every finding.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import date, datetime, timedelta

from ..config.schema import ObliskConfig
from ..data.candidates import Candidate, candidate_cells
from ..domain.enums import AssetClass
from ..domain.orders import TERMINAL_STATES
from ..domain.thesis import (
    UNSTORED,
    Thesis,
    ThesisDraft,
    ThesisInstrument,
    ThesisState,
)
from ..engines.thesis.gate import Finding, Reason, screen, view_findings
from ..store import orders as repo
from ..store import theses as thesis_repo
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

    def _from_universe(self, symbol: str) -> ThesisInstrument | list[Finding]:
        settings = self._settings
        declared = settings.universe.instrument(symbol)
        if declared is None:
            return _refused(
                Reason.INSTRUMENT_UNKNOWN,
                f"{symbol} is not in config/universe.yaml; a share outside it is named by its "
                "stock candidate (--candidate), a fund through a reviewed universe entry",
            )
        if not declared.tradable or declared.conid is None or declared.description is None:
            why = declared.data_only_reason or "it is not pinned and described yet"
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
                f"no stock candidate {key!r} as of {today.isoformat()} (obliskiq universe "
                "candidates lists them)",
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

    def _submit(self, store: Store, draft: ThesisDraft, actor: str) -> Thesis | list[Finding]:
        at = self._now()
        today = at.date()
        findings = screen(draft, today=today)
        if findings:
            return findings
        instrument = (
            self._from_universe(draft.symbol)
            if draft.symbol is not None
            else self._from_candidate(store, draft.candidate or "", draft.isin or "", today)
        )
        if isinstance(instrument, list):
            return instrument
        findings = view_findings(draft.view, instrument)
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
