"""The link from a thesis of the thesis book to the options sleeve (MASTER_PLAN §5.5, §5.6;
DECISIONS D157, D167): which contract, how many, and the review of the position it becomes.

A thesis hands the sleeve its instrument (the underlying), its target, its horizon and its
direction: a target above the invalidation is a long call, one below it a long put. Nothing here
is a second set of rules. :meth:`ThesisLink.choose` puts the underlying's newest chain through the
sleeve's own gates (``decision/gates.py``: the instrument, the tenor window, the mandatory close,
liquidity, the smile, the concurrency cap, the pool, the implied-vol percentile, the payoff at
the thesis's target, the entry's place in its range, the theta budget to the horizon) and adds
what a thesis asks of a tenor (the option is still held on the horizon) and the friction gate.

* **The pool** is the sleeve's (``penalty.py``): the budget less the penalty balance and the
  open commitments. The budget is ``capital.budgets.option_premium`` of investable capital, and
  never more than the thesis book's own capital, which pays the premium.
* **One block**, in whole contracts: ``option_sleeve.block_share`` of the budget, at least one
  contract, inside the pool and what the book may spend now.
* **Every gate tested.** A contract with an untested gate is not bought: an option is taken only
  on rules that were applied. Why none qualified is said by gate.
* **The choice** among the contracts that qualify is the one whose payoff at the thesis's target
  is the largest multiple of its premium (the gate's own measure), the nearer expiry on a tie.
* **Activation** is the thesis's (:func:`.activation.thesis_activation`), not H8's.

:meth:`ThesisLink.review` reads what the zombie test needs for an open position off the newest
chain. Everything reads the store as of a day and writes nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ...calendars.service import Calendars
from ...capital.budgets import SleeveBudgets
from ...config.schema import ObliskConfig
from ...data.macro.catalog import MacroCatalog
from ...decision.gates import (
    Affordability,
    CandidateGates,
    Gate,
    GateResult,
    OptionCandidate,
    OptionEntry,
)
from ...domain.enums import OptionRight
from ...domain.options import OptionPosition
from ...domain.orders import Sleeve
from ...domain.thesis import Thesis
from ...grid.frames import ChainSmile, MacroReader
from ...grid.option_surface import underlying_spot
from ...quant.smile import SmileFilters
from ...store import options as options_repo
from ...store import repos
from ...store.db import Store
from ..fx.rates import BaseRates
from ..thesis.gate import Finding, Reason
from .activation import Activation, FrictionGate, FrictionReading, thesis_activation
from .candidates import CandidateBook, Pool
from .penalty import ZERO, PenaltyLedger, SleeveStatus
from .zombie import OptionRisk, ZombieTest

#: How the friction gate is counted among the gates that refused a contract.
FRICTION = "friction"
#: Where the dividend yield of a review or an entry came from.
Q_COLLECTED = "collected"
Q_NONE = "none collected, priced at zero"


@dataclass(frozen=True, slots=True)
class Chosen:
    """The contract a thesis is held as, and what was read to choose it.

    ``premium_base`` is the block's premium at the chain's mid, in the base currency;
    ``payoff_ratio`` the intrinsic value at the thesis's target over the premium; ``spot`` the
    underlying's price at the chain's session.
    """

    candidate: OptionCandidate
    contracts: int
    premium_base: Decimal
    payoff_ratio: float
    prob_target: float | None
    spot: float
    result: GateResult
    friction: FrictionReading
    status: SleeveStatus
    activation: Activation

    def summary(self) -> str:
        row = self.candidate.row
        kind = "call" if row.right is OptionRight.CALL else "put"
        return (
            f"{self.contracts} x {self.candidate.underlying.symbol} {row.expiry.isoformat()} "
            f"{row.strike:g} {kind} at about {row.mid}: {self.premium_base:.2f} of premium, "
            f"{self.payoff_ratio:.1f}x at the target"
        )


@dataclass(frozen=True, slots=True)
class _Inputs:
    """What every contract of one chain is judged on besides its own quote."""

    spot: float | None
    rate: float | None
    dividend_yield: float
    dividend_basis: str
    range_place: float | None
    range_gap: str | None


class ThesisLink:
    """Chooses the option a thesis is held as and reads its reviews, bound to configuration."""

    def __init__(self, settings: ObliskConfig, calendars: Calendars) -> None:
        self._settings = settings
        self._calendars = calendars
        self._gates = CandidateGates(settings)
        self._friction = FrictionGate(settings)
        self._budget_share = SleeveBudgets(settings.capital).share(Sleeve.OPTIONS)
        self._block_share = settings.option_sleeve.block_share
        self._filters = SmileFilters.from_config(settings)
        self._freshness = settings.grid.freshness_days.option_chain
        self._rates = settings.numerics.implied_vol.solve.risk_free_series
        self._range_sessions = settings.discipline.entry_range_sessions
        self._venues = {code: entry.venue for code, entry in settings.ibkr_exchanges.items()}

    # ── the pool ──

    def budget(self, *, investable: Decimal, book_capital: Decimal) -> Decimal:
        """The option budget a thesis's premium comes out of: the option share of investable
        capital, never above the thesis book's own capital."""
        return min(investable * self._budget_share, book_capital)

    def status(
        self, store: Store, *, as_of: date, investable: Decimal, book_capital: Decimal
    ) -> SleeveStatus:
        """The pool on ``as_of``: the budget, the penalty balance, the commitments, what is
        left."""
        return PenaltyLedger(store, self._settings).status(
            budget=self.budget(investable=investable, book_capital=book_capital), as_of=as_of
        )

    # ── the choice ──

    def choose(
        self,
        store: Store,
        thesis: Thesis,
        *,
        as_of: date,
        investable: Decimal,
        book_capital: Decimal,
        spendable: Decimal,
    ) -> Chosen | list[Finding]:
        """The contract ``thesis`` is held as on ``as_of``, or why none is bought. A permanent
        finding says the instrument has no options to buy at all."""
        conid = thesis.instrument.conid
        symbol = thesis.instrument.symbol
        declared = None if conid is None else self._settings.universe.pinned(conid)
        if conid is None or declared is None or not declared.collects_chain:
            why = None if declared is None else declared.options_unavailable_reason
            return [
                Finding(
                    Reason.NO_OPTIONS,
                    f"no option chain is collected on {symbol}" + (f": {why}" if why else ""),
                    permanent=True,
                )
            ]
        status = self.status(store, as_of=as_of, investable=investable, book_capital=book_capital)
        activation = thesis_activation(
            book_capital=book_capital, available=status.available if book_capital > ZERO else None
        )
        if not activation.active:
            locked = f" (locked by {status.locked_by})" if status.locked_by else ""
            return [
                Finding(
                    Reason.OPTION_BUDGET,
                    f"{activation.reason()}: a budget of {status.budget:.2f}, a penalty balance "
                    f"of {status.penalty_balance:.2f} and {status.committed_premium:.2f} of "
                    f"committed premium{locked}",
                )
            ]
        affordability = self._gates.affordability(
            budget=status.budget,
            penalty_balance=status.penalty_balance,
            committed_premium=status.committed_premium,
        )
        pool = Pool(
            affordability=affordability,
            affordability_gap=None,
            open_positions=len(options_repo.open_positions(store, as_of=as_of)),
            open_positions_gap=None,
        )
        book = CandidateBook(store, self._settings, self._calendars)
        right = OptionRight.PUT if thesis.falls else OptionRight.CALL
        kind = "put" if thesis.falls else "call"
        candidates = [
            candidate
            for candidate in book.candidates(as_of=as_of, pool=pool, underlying_conid=conid)
            if candidate.row.right is right
        ]
        if not candidates:
            stale = next((s.reason for s in book.skipped if s.underlying_conid == conid), None)
            return [
                Finding(
                    Reason.NO_OPTIONS,
                    stale or f"no {kind} of {symbol} is in a chain collected by {as_of}",
                )
            ]
        inputs = self._inputs(store, conid, declared.currency, as_of, falls=thesis.falls)
        rates = BaseRates(store, self._settings, as_of=as_of)
        zombie = ZombieTest(store, self._settings)
        refused: Counter[str] = Counter()
        untested: Counter[str] = Counter()
        passed: list[Chosen] = []
        for candidate in candidates:
            judged = self._judge(
                zombie, thesis, candidate, inputs, affordability, spendable, rates, status,
                activation,
            )  # fmt: skip
            if isinstance(judged, Chosen):
                passed.append(judged)
            else:
                failed, open_gates = judged
                refused.update(set(failed))
                untested.update(set(open_gates))
        if not passed:
            return [_none_qualified(kind, symbol, len(candidates), refused, untested)]
        return max(
            passed,
            key=lambda c: (
                c.payoff_ratio,
                -c.candidate.row.expiry.toordinal(),
                -c.candidate.row.strike,
            ),
        )

    def _judge(
        self,
        zombie: ZombieTest,
        thesis: Thesis,
        candidate: OptionCandidate,
        inputs: _Inputs,
        affordability: Affordability,
        spendable: Decimal,
        rates: BaseRates,
        status: SleeveStatus,
        activation: Activation,
    ) -> Chosen | tuple[list[str], list[str]]:
        """One contract through every gate at the size its block would have: the choice it would
        be, or the gates that refused it and those left untested."""
        row = candidate.row
        is_call = row.right is OptionRight.CALL
        target = float(thesis.target)
        unit = None
        if row.mid is not None and row.mid > 0 and row.multiplier is not None:
            unit = self._gates.cost_in_base(
                Decimal(repr(row.mid)) * Decimal(repr(row.multiplier)),
                currency=candidate.currency,
                fx_to_base=candidate.fx_to_base,
            )
        contracts = 1
        if unit is not None and unit > ZERO:
            block = status.budget * self._block_share
            ceiling = min(affordability.available, spendable)
            contracts = max(1, min(int(block // unit), int(ceiling // unit)))
        theta, theta_gap = self._theta(zombie, thesis, candidate, inputs)
        terms = OptionEntry(
            contracts=contracts,
            target=target,
            entry_range_pctile=inputs.range_place,
            theta_remaining_share=theta,
            theta_gap=theta_gap,
            entry_range_gap=inputs.range_gap,
            horizon=thesis.horizon,
        )
        result = self._gates.entry(candidate, terms)
        failed = [gate.value for gate in result.failed_gates]
        open_gates = [gate.value for gate in result.untested_gates]
        if row.option_conid is None:
            open_gates.append(Gate.INSTRUMENT.value)
        code = row.option_exchange or candidate.underlying.option_exchange
        venue = None if code is None else self._venues.get(code)
        friction = None
        if unit is not None and row.mid is not None and row.multiplier is not None and venue:
            friction = self._friction.assess(
                premium_per_unit=Decimal(repr(row.mid)),
                multiplier=Decimal(repr(row.multiplier)),
                contracts=contracts,
                venue=venue,
                currency=candidate.currency,
                rate_to_base=rates.to_base,
            )
        holds = None if friction is None else self._friction.holds(friction)
        if holds is None:
            open_gates.append(FRICTION)
        elif not holds:
            failed.append(FRICTION)
        if unit is not None and contracts * unit > spendable:
            failed.append(Gate.POSITION_SIZE.value)
        if failed or open_gates or unit is None or friction is None or row.mid is None:
            return failed, open_gates
        assert inputs.spot is not None  # the theta budget was computed on it
        intrinsic = (target - row.strike) if is_call else (row.strike - target)
        return Chosen(
            candidate=candidate,
            contracts=contracts,
            premium_base=contracts * unit,
            payoff_ratio=intrinsic / row.mid,
            prob_target=self._probability(zombie, thesis, candidate, inputs),
            spot=inputs.spot,
            result=result,
            friction=friction,
            status=status,
            activation=activation,
        )

    def _inputs(
        self, store: Store, conid: int, currency: str, as_of: date, *, falls: bool
    ) -> _Inputs:
        session = repos.latest_chain_date(store, as_of=as_of, underlying_conid=conid) or as_of
        spot = underlying_spot(store, conid, session)
        rate = self._rate(store, currency, session)
        q, basis = self._dividend_yield(store, conid, session)
        place, gap = self._range_place(store, conid, session, spot)
        if place is not None and falls:
            # A put bought at the bottom of the range is the mirror of a call bought at the top.
            place = 1 - place
        return _Inputs(spot, rate, q, basis, place, gap)

    def _rate(self, store: Store, currency: str, session: date) -> float | None:
        series_id = self._rates.get(currency)
        if series_id is None:
            return None
        reader = MacroReader(store, MacroCatalog.from_settings(self._settings), session)
        return reader.value_at(series_id, session)

    @staticmethod
    def _dividend_yield(store: Store, conid: int, session: date) -> tuple[float, str]:
        snapshot = repos.snapshots(store, conid, as_of=session, limit=1)
        if snapshot and snapshot[-1].dividend_yield is not None:
            return snapshot[-1].dividend_yield, Q_COLLECTED
        return 0.0, Q_NONE

    def _range_place(
        self, store: Store, conid: int, session: date, spot: float | None
    ) -> tuple[float | None, str | None]:
        """Where ``spot`` sits between the lowest and the highest close of the last
        ``discipline.entry_range_sessions`` stored sessions (0 the low, 1 the high)."""
        bars = repos.bar_series(store, conid, as_of=session, limit=self._range_sessions)
        if spot is None or len(bars) < self._range_sessions:
            return None, (
                f"{len(bars)} of the {self._range_sessions} stored sessions the entry's place in "
                "its range is measured on (discipline.entry_range_sessions)"
            )
        low = min(bar.close for bar in bars)
        high = max(bar.close for bar in bars)
        if high <= low:
            return None, "the stored closes span no range"
        return min(1.0, max(0.0, (spot - low) / (high - low))), None

    def _theta(
        self, zombie: ZombieTest, thesis: Thesis, candidate: OptionCandidate, inputs: _Inputs
    ) -> tuple[float | None, str | None]:
        """What the premium keeps if the thesis takes until its horizon with nothing else
        moving."""
        row = candidate.row
        if candidate.smile is None or inputs.spot is None or row.mid is None or row.mid <= 0:
            return None, "no smile, spot or premium to compute the theta budget on"
        if inputs.rate is None:
            return None, (
                f"no risk-free rate for {candidate.currency} as of the chain's session "
                "(numerics.implied_vol.solve.risk_free_series)"
            )
        budget = zombie.theta_budget(
            spot=inputs.spot,
            strike=row.strike,
            is_call=row.right is OptionRight.CALL,
            sigma=candidate.smile.iv_at(row.strike).iv,
            rate=inputs.rate,
            dividend_yield=inputs.dividend_yield,
            premium_per_unit=row.mid,
            entry_dte=(row.expiry - candidate.as_of).days,
            expected_resolution_days=max(0, (thesis.horizon - candidate.as_of).days),
        )
        return budget.remaining_share, None

    def _probability(
        self, zombie: ZombieTest, thesis: Thesis, candidate: OptionCandidate, inputs: _Inputs
    ) -> float | None:
        """N(d2) at the thesis's target on the entry's smile: what the zombie test later compares
        the position's odds with."""
        row = candidate.row
        if candidate.smile is None or inputs.spot is None or inputs.rate is None or row.mid is None:
            return None
        risk = zombie.assess(
            as_of=candidate.as_of,
            spot=inputs.spot,
            strike=row.strike,
            is_call=row.right is OptionRight.CALL,
            expiry=row.expiry,
            premium_per_unit=row.mid,
            bid=row.bid,
            smile=candidate.smile,
            rate=inputs.rate,
            dividend_yield=inputs.dividend_yield,
            dividend_basis=inputs.dividend_basis,
            stated_target=float(thesis.target),
        )
        return risk.prob_target

    # ── the review of an open position ──

    def review(self, store: Store, position: OptionPosition, *, as_of: date) -> OptionRisk | str:
        """What the zombie test reads for ``position`` off the underlying's newest chain on or
        before ``as_of``; why, when the session gives no reading (the position then runs on to
        its other exits)."""
        conid = position.underlying_conid
        chain = ChainSmile(store, conid, as_of, None, self._filters)
        rows = chain.rows()
        session = chain.session
        if session is None or not rows:
            return f"no chain of {conid} is collected by {as_of}"
        if (as_of - session).days > self._freshness:
            return f"the newest chain of {conid} is from {session}: too old to review on"
        if session < position.opened_on:
            return f"no chain of {conid} has been collected since the position opened"
        spot = underlying_spot(store, conid, session)
        if spot is None:
            return f"no price of {conid} at the chain's session {session}"
        priced = ChainSmile(store, conid, as_of, spot, self._filters)
        smile = priced.at_expiry(position.expiry)
        if smile is None:
            return f"no smile survived hygiene at the {position.expiry} expiry on {session}"
        rate = self._rate(store, position.currency, session)
        if rate is None:
            return f"no risk-free rate for {position.currency} as of {session}"
        q, basis = self._dividend_yield(store, conid, session)
        row = next(
            (
                r
                for r in rows
                if r.expiry == position.expiry
                and r.right is position.right
                and r.strike == position.strike
            ),
            None,
        )
        return ZombieTest(store, self._settings).assess(
            as_of=session,
            spot=spot,
            strike=position.strike,
            is_call=position.is_call,
            expiry=position.expiry,
            premium_per_unit=float(position.premium_per_unit),
            bid=None if row is None else row.bid,
            smile=smile,
            rate=rate,
            dividend_yield=q,
            dividend_basis=basis,
            prob_target_at_entry=position.prob_target_at_entry,
            stated_target=position.target,
        )


def _none_qualified(
    kind: str, symbol: str, scanned: int, refused: Counter[str], untested: Counter[str]
) -> Finding:
    parts = [f"none of the {scanned} {kind}s of {symbol} cleared the sleeve's gates"]
    if refused:
        parts.append(
            "refused by " + ", ".join(f"{gate} {count}" for gate, count in refused.most_common())
        )
    if untested:
        parts.append(
            "untested: " + ", ".join(f"{gate} {count}" for gate, count in untested.most_common())
        )
    return Finding(Reason.OPTION_GATES, "; ".join(parts))
