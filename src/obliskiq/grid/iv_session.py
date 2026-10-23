"""The session's implied-volatility solve: one at-the-money implied vol per underlying where the
route served none, written to its own column with its method.

Ported from lite's ``compute/iv_solve.py`` (``solve_session``) on Session 2's pure solve
(``quant/iv_solve.py``). A chain carrying prices and no implied vol is inverted at its mid prices;
this layer holds what a collector does not: the discount rate known on the chain's own session and
the underlying's collected dividend yield. What it refuses, each a quietly wrong number:

* **Writing into the vendor's column.** ``implied_vol_solved`` is its own column, labelled with
  the method (``solved_bsm_european``) and the dividend-yield tier, so a percentile never ranks a
  series whose halves were measured two ways.
* **Solving a chain the route priced.** A route that served an implied vol is not improved by
  inverting its own mid prices.
* **Discounting at a rate nobody chose.** The rate is the currency's series
  (``numerics.implied_vol.solve.risk_free_series``) as released by the chain's session; an
  underlying whose currency has none, or none known then, is left unsolved.
* **Reporting a sigma it did not find.** Non-convergence is counted, never clamped; past
  ``max_non_converged_fraction`` the run is degraded.

The solved smile passes the same hygiene as the quoted one before its ATM value is written.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Final

from ..config.schema import ObliskConfig
from ..data.macro.catalog import MacroCatalog
from ..quant.bsm import BsmInputs, IvSolverInputs
from ..quant.iv_solve import (
    SOURCE_SOLVED_BSM_EUROPEAN,
    SolveInputs,
    SolveReport,
    select_contracts,
    solve_rows,
)
from ..quant.smile import Smile, SmileFilters
from ..store import repos
from ..store.db import Store
from .frames import MacroReader

#: The dividend-yield tier when nothing was collected: zero, and labelled so (q shifts an ATM
#: sigma only slightly at these tenors, and refusing would leave every underlying unsolved
#: until a yield is collected).
Q_NONE: Final = "none_collected"
Q_COLLECTED: Final = "collected"


class Skip(StrEnum):
    """Why an underlying was not solved, counted per run."""

    NO_CHAIN = "no_chain"
    VENDOR_SERVED = "vendor_already_served_iv"
    NO_SPOT = "no_spot"
    NO_INSTRUMENT = "instrument_not_seeded"
    NO_RATE = "no_rate_as_of_session"
    NO_CONTRACTS = "no_contracts_in_band"
    NO_SMILE = "no_smile_survived_hygiene"


class SessionSolver:
    """Solves and stores one ATM implied vol per underlying for a session."""

    def __init__(self, store: Store, settings: ObliskConfig) -> None:
        self._store = store
        self._settings = settings
        self._inputs = SolveInputs.from_config(settings)
        self._bsm = BsmInputs.from_config(settings)
        self._solver = IvSolverInputs.from_config(settings)
        self._filters = SmileFilters.from_config(settings)
        self._catalog = MacroCatalog.from_settings(settings)
        self._rates = settings.numerics.implied_vol.solve.risk_free_series

    def run(self, *, as_of: date, at: datetime, write: bool = True) -> SolveReport:
        report = SolveReport(as_of=as_of)
        for conid in repos.chain_underlyings(self._store, as_of=as_of):
            self._solve_one(conid, as_of=as_of, at=at, report=report, write=write)
        return report

    def _solve_one(
        self, conid: int, *, as_of: date, at: datetime, report: SolveReport, write: bool
    ) -> None:
        store = self._store
        session = repos.latest_chain_date(store, as_of=as_of, underlying_conid=conid)
        rows = (
            ()
            if session is None
            else repos.chain_rows(store, as_of=as_of, underlying_conid=conid, snapshot_date=session)
        )
        if session is None or not rows:
            report.skip(Skip.NO_CHAIN)
            return
        if any(row.iv is not None for row in rows):
            report.skip(Skip.VENDOR_SERVED)
            return
        spot = self._spot(conid, session)
        if spot is None or spot <= 0:
            report.skip(Skip.NO_SPOT)
            return
        instrument = repos.get_instrument(store, conid, as_of=session)
        if instrument is None:
            report.skip(Skip.NO_INSTRUMENT)
            return
        rate = self._rate(instrument.currency, session)
        if rate is None:
            report.skip(Skip.NO_RATE)
            return
        q, tier = self._dividend_yield(conid, session)
        candidates = select_contracts(rows, spot=spot, session=session, inputs=self._inputs)
        if not candidates:
            report.skip(Skip.NO_CONTRACTS)
            return
        solved = solve_rows(
            candidates,
            spot=spot,
            session=session,
            rate=rate,
            dividend_yield=q,
            bsm=self._bsm,
            solver=self._solver,
        )
        report.add(solved)
        smile = (
            None
            if not solved.rows
            else Smile.from_rows(
                solved.rows,
                spot=spot,
                underlying_conid=conid,
                expiry=solved.rows[0].expiry,
                filters=self._filters,
            )
        )
        if smile is None:
            report.skip(Skip.NO_SMILE)
            return
        if write:
            repos.upsert_solved_implied_vol(
                store,
                conid=conid,
                snapshot_date=session,
                value=smile.atm().iv,
                method=f"{SOURCE_SOLVED_BSM_EUROPEAN}/q_{tier}",
                route=rows[0].source,
                at=at,
            )
        report.solved += 1

    def _spot(self, conid: int, session: date) -> float | None:
        """Spot at the chain's own session, never a later one: the snapshot's last, else the
        session's close."""
        snapshot = repos.snapshots(self._store, conid, as_of=session, limit=1)
        if snapshot and snapshot[-1].last is not None:
            return snapshot[-1].last
        bars = repos.bar_series(self._store, conid, as_of=session, limit=1)
        return bars[-1].close if bars else None

    def _rate(self, currency: str, session: date) -> float | None:
        series_id = self._rates.get(currency)
        if series_id is None:
            return None
        return MacroReader(self._store, self._catalog, session).value_at(series_id, session)

    def _dividend_yield(self, conid: int, session: date) -> tuple[float, str]:
        snapshot = repos.snapshots(self._store, conid, as_of=session, limit=1)
        if snapshot and snapshot[-1].dividend_yield is not None:
            return snapshot[-1].dividend_yield, Q_COLLECTED
        return 0.0, Q_NONE
