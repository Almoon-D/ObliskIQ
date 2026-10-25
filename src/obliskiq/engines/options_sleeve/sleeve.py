"""The options sleeve (MASTER_PLAN §5.6): its pool, its candidates through the gates, its
activation condition, and the would-be entries it logs while that condition fails.

* **The budget** is ``capital.budgets.option_premium`` of current investable capital (Session 7's
  one reader, ``SleeveBudgets``): it falls when capital falls. lite's ``budget_pct`` is not a key
  here (DECISIONS D135). The pool is that budget less the penalty balance and the premium open
  positions commit (:mod:`.penalty`).
* **Candidates** are every contract of each underlying's newest chain, put through the candidate
  gates once (``decision/gates.py``): screen, liquidity, smile, tenor, the mandatory close, the
  concurrency cap and the pool.
* **Activation** (:mod:`.activation`): H8 sufficient, the budget buying the pre-registered number
  of median premiums, each within the friction gate. Until it holds the sleeve is in shadow: it
  trades nothing, and every contract that failed no gate is logged with its quote, for H8.

The sleeve places nothing: Session 17's decision layer turns an active sleeve's entries into
intents. Every input that cannot be read leaves its gate or its leg untested, with why.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ...calendars.service import Calendars
from ...capital.budgets import SleeveBudgets
from ...config.schema import ObliskConfig
from ...decision.gates import CandidateGates, Gate, GateResult, OptionCandidate
from ...domain.options import ShadowEntry
from ...domain.orders import Sleeve
from ...domain.research import RegisteredHypothesis
from ...store import options as options_repo
from ...store.db import Store
from ...util.timeutil import utc_now
from ..fx.rates import BaseRates
from .activation import Activation, FrictionGate, FrictionReading, SleeveActivation
from .candidates import CandidateBook, Pool, Skipped
from .penalty import PenaltyLedger, SleeveStatus


@dataclass(frozen=True, slots=True)
class Evaluated:
    """One candidate and its gates' verdict."""

    candidate: OptionCandidate
    result: GateResult
    premium_base: Decimal | None


@dataclass(frozen=True, slots=True)
class SleeveRun:
    """One session of the sleeve: its pool, its candidates through the gates, its activation."""

    as_of: date
    status: SleeveStatus | None
    budget_gap: str | None
    scanned: int
    survivors: tuple[Evaluated, ...]
    failed: Counter[Gate]
    untested: Counter[Gate]
    median_premium: Decimal | None
    friction: FrictionReading | None
    activation: Activation
    skipped: tuple[Skipped, ...]

    @property
    def fully_tested(self) -> int:
        return sum(1 for item in self.survivors if item.result.fully_tested)

    def report(self) -> list[str]:
        lines = [
            f"options sleeve on {self.as_of}: {self.activation.reason()}",
            f"  {self.scanned} contracts through the gates, {len(self.survivors)} failed none "
            f"({self.fully_tested} with every gate applied)",
        ]
        if self.status is not None:
            status = self.status
            lines.append(
                f"  pool: budget {status.budget:.2f}, penalty {status.penalty_balance:.2f}, "
                f"committed {status.committed_premium:.2f}, available {status.available:.2f}"
                + (f" (locked by {status.locked_by})" if status.locked_by else "")
            )
        elif self.budget_gap:
            lines.append(f"  pool: untested ({self.budget_gap})")
        if self.failed:
            refused = ", ".join(
                f"{gate.value} {count}" for gate, count in self.failed.most_common()
            )
            lines.append(f"  refused by: {refused}")
        if self.untested:
            open_gates = ", ".join(
                f"{gate.value} {count}" for gate, count in self.untested.most_common()
            )
            lines.append(f"  untested: {open_gates}")
        lines += [f"  skipped {item.underlying_conid}: {item.reason}" for item in self.skipped]
        return lines


class OptionsSleeve:
    """The sleeve over one store, bound to configuration once."""

    def __init__(
        self,
        store: Store,
        settings: ObliskConfig,
        *,
        calendars: Calendars | None,
        evidence: RegisteredHypothesis | None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._settings = settings
        self._now = now
        self._gates = CandidateGates(settings)
        self._budgets = SleeveBudgets(settings.capital)
        self._penalty = PenaltyLedger(store, settings, now=now)
        self._book = CandidateBook(store, settings, calendars)
        self._activation = SleeveActivation(settings)
        self._friction = FrictionGate(settings)
        self._evidence = evidence
        self._venues = {code: entry.venue for code, entry in settings.ibkr_exchanges.items()}

    @property
    def penalty(self) -> PenaltyLedger:
        return self._penalty

    def budget(self, investable: Decimal) -> Decimal:
        """The sleeve's gross budget on ``investable`` capital (``capital.budgets
        .option_premium``)."""
        return investable * self._budgets.share(Sleeve.OPTIONS)

    def evaluate(
        self, *, as_of: date, investable: Decimal | None, investable_gap: str | None = None
    ) -> SleeveRun:
        """Every candidate of the session through the gates, the pool on ``investable`` capital
        (``None`` with ``investable_gap`` when it is not known), and the activation."""
        status = None
        if investable is not None:
            status = self._penalty.status(budget=self.budget(investable), as_of=as_of)
        affordability = (
            None
            if status is None
            else self._gates.affordability(
                budget=status.budget,
                penalty_balance=status.penalty_balance,
                committed_premium=status.committed_premium,
            )
        )
        open_positions = len(options_repo.open_positions(self._store, as_of=as_of))
        pool = Pool(
            affordability=affordability,
            affordability_gap=investable_gap,
            open_positions=open_positions,
            open_positions_gap=None,
        )
        failed: Counter[Gate] = Counter()
        untested: Counter[Gate] = Counter()
        survivors: list[Evaluated] = []
        scanned = 0
        for candidate in self._book.candidates(as_of=as_of, pool=pool):
            scanned += 1
            result = self._gates.candidate(candidate)
            failed.update(set(result.failed_gates))
            untested.update(result.untested_gates)
            if result.passed:
                survivors.append(Evaluated(candidate, result, self._premium_base(candidate)))
        median, median_gap = self._median(survivors)
        friction = self._median_friction(survivors, median, as_of)
        activation = self._activation.evaluate(
            evidence=self._evidence,
            budget=None if status is None else status.budget,
            budget_gap=investable_gap,
            median_premium=median,
            median_gap=median_gap,
            friction=friction,
            friction_holds=None if friction is None else self._friction.holds(friction),
        )
        return SleeveRun(
            as_of=as_of,
            status=status,
            budget_gap=investable_gap if status is None else None,
            scanned=scanned,
            survivors=tuple(survivors),
            failed=failed,
            untested=untested,
            median_premium=median,
            friction=friction,
            activation=activation,
            skipped=tuple(self._book.skipped),
        )

    def log_shadow(self, run: SleeveRun) -> int:
        """Log the session's would-be entries while the sleeve is in shadow (none when it is
        active: an active sleeve's entries are the decision layer's)."""
        if run.activation.active:
            return options_repo.replace_shadow_entries(self._store, run.as_of, [])
        reason = run.activation.reason()
        at = self._now()
        entries = [
            ShadowEntry(
                as_of=run.as_of,
                underlying_conid=item.candidate.row.underlying_conid,
                expiry=item.candidate.row.expiry,
                strike=item.candidate.row.strike,
                right=item.candidate.row.right,
                snapshot_date=item.candidate.row.snapshot_date,
                option_conid=item.candidate.row.option_conid,
                bid=item.candidate.row.bid,
                ask=item.candidate.row.ask,
                mid=mid,
                iv=item.candidate.row.iv,
                delta=item.candidate.row.delta,
                multiplier=item.candidate.row.multiplier,
                currency=item.candidate.currency,
                premium_base=item.premium_base,
                iv_percentile=item.candidate.iv_percentile,
                market_data_status=item.candidate.row.market_data_status,
                untested=tuple(gate.value for gate in item.result.untested_gates),
                activation=reason,
                computed_at=at,
            )
            for item in run.survivors
            if (mid := item.candidate.row.mid) is not None and mid > 0
        ]
        return options_repo.replace_shadow_entries(self._store, run.as_of, entries)

    # ── internals ──

    def _premium_base(self, candidate: OptionCandidate) -> Decimal | None:
        """One contract's premium in the base currency; ``None`` without a quote, a multiplier
        or a rate."""
        row = candidate.row
        if row.mid is None or row.mid <= 0 or row.multiplier is None:
            return None
        local = Decimal(repr(row.mid)) * Decimal(repr(row.multiplier))
        return self._gates.cost_in_base(
            local, currency=candidate.currency, fx_to_base=candidate.fx_to_base
        )

    @staticmethod
    def _median(survivors: list[Evaluated]) -> tuple[Decimal | None, str | None]:
        priced = [item.premium_base for item in survivors if item.premium_base is not None]
        if not priced:
            reason = (
                "no contract failed no gate, so there is no median premium"
                if not survivors
                else f"none of the {len(survivors)} contracts that failed no gate could be priced "
                "in the base currency (a quote, a multiplier and a rate are all needed)"
            )
            return None, reason
        return statistics.median(priced), None

    def _median_friction(
        self, survivors: list[Evaluated], median: Decimal | None, as_of: date
    ) -> FrictionReading | None:
        """The round trip of one contract at the median premium, on the venue and in the
        currency of the contract nearest it."""
        if median is None:
            return None
        priced = [item for item in survivors if item.premium_base is not None]
        nearest = min(priced, key=lambda item: abs((item.premium_base or median) - median))
        row = nearest.candidate.row
        code = row.option_exchange or nearest.candidate.underlying.option_exchange
        venue = None if code is None else self._venues.get(code)
        if venue is None or row.mid is None or row.multiplier is None:
            return FrictionReading(
                median,
                None,
                None,
                None,
                f"the median contract's option exchange {code} maps to no venue to cost it on",
            )
        rates = BaseRates(self._store, self._settings, as_of=as_of)
        return self._friction.assess(
            premium_per_unit=Decimal(repr(row.mid)),
            multiplier=Decimal(repr(row.multiplier)),
            contracts=1,
            venue=venue,
            currency=nearest.candidate.currency,
            rate_to_base=rates.to_base,
        )
