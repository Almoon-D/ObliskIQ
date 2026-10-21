"""The tax ledger: what each tax year realised, and the tax it owes once settled.

The ledger keeps two components per year, as a savings base does: **income** (dividends and
interest, with what was withheld abroad) and **gains** (realised gains and losses, deferred and
released losses included). A year is settled once, in order, when its tax is due:

1. a negative component offsets the other's positive balance, up to the policy's cross-offset
   fraction of it;
2. negative balances carried from earlier years are used, oldest first, in the same order
   (their own component first, then the other within what is left of the same fraction), and a
   balance older than the policy's carry-forward years expires;
3. what is still negative is carried out; the rest is the base, taxed on the policy's scale,
   less the credit for tax withheld abroad.

A settled year is closed: an event that would change it (a deferral discovered after the fact)
is refused, never folded silently into another year.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections import defaultdict
from dataclasses import dataclass, field, replace
from decimal import Decimal

from ..domain.tax import (
    CarryForward,
    Component,
    IncomeEvent,
    LedgerState,
    LedgerYear,
    Realisation,
    TaxYearSummary,
)
from ..errors import TaxError
from ..jurisdiction.tax import TaxPolicy

ZERO = Decimal(0)


@dataclass(slots=True)
class _Year:
    income: Decimal = ZERO
    gains: Decimal = ZERO
    withheld: Decimal = ZERO
    foreign_income: Decimal = ZERO


@dataclass(slots=True)
class _Carry:
    component: Component
    arising_year: int
    amount: Decimal


@dataclass(slots=True)
class _Caps:
    """What is left of each component's cross-offset allowance this year."""

    from_income: Decimal
    from_gains: Decimal


@dataclass(slots=True)
class _Balances:
    income: Decimal
    gains: Decimal
    caps: _Caps
    used: Decimal = ZERO
    expired: Decimal = ZERO
    carried: list[_Carry] = field(default_factory=list)


class TaxLedger:
    """Realised results by tax year, settled in order under one policy."""

    def __init__(self, policy: TaxPolicy) -> None:
        self._policy = policy
        self._years: defaultdict[int, _Year] = defaultdict(_Year)
        self._carry: list[_Carry] = []
        self._settled: dict[int, TaxYearSummary] = {}

    def copy(self) -> TaxLedger:
        """An independent ledger in the same state (the hooks try a trade on one)."""
        twin = TaxLedger(self._policy)
        for year, row in self._years.items():
            twin._years[year] = replace(row)
        twin._carry = [replace(carry) for carry in self._carry]
        twin._settled = dict(self._settled)  # summaries are immutable
        return twin

    def snapshot(self) -> LedgerState:
        """Everything the ledger holds, to be saved whole."""
        return LedgerState(
            years=tuple(
                LedgerYear(
                    tax_year=year,
                    income=row.income,
                    gains=row.gains,
                    withheld=row.withheld,
                    foreign_income=row.foreign_income,
                )
                for year, row in sorted(self._years.items())
            ),
            carried=tuple(
                CarryForward(component=c.component, arising_year=c.arising_year, amount=c.amount)
                for c in self._carry
            ),
            settled=self.settled(),
        )

    def restore(self, state: LedgerState) -> None:
        """Load a saved state into a ledger that holds nothing (a restart from the store)."""
        if self._years or self._carry or self._settled:
            raise TaxError("a ledger can be restored only when it holds nothing")
        for year in state.years:
            self._years[year.tax_year] = _Year(
                income=year.income,
                gains=year.gains,
                withheld=year.withheld,
                foreign_income=year.foreign_income,
            )
        self._carry = [_Carry(c.component, c.arising_year, c.amount) for c in state.carried]
        self._settled = {summary.tax_year: summary for summary in state.settled}

    # ── recording ──

    def ensure_open(self, year: int) -> None:
        """Refuse, before anything changes, an event dated in a settled or earlier year."""
        if year in self._settled:
            raise TaxError(f"tax year {year} is settled; an event dated in it cannot be added")
        last = max(self._settled, default=None)
        if last is not None and year < last:
            raise TaxError(f"tax year {year} precedes the settled year {last}")

    def _open_year(self, year: int) -> _Year:
        self.ensure_open(year)
        return self._years[year]

    def record_realisation(self, realisation: Realisation) -> None:
        self._open_year(realisation.disposed_on.year).gains += realisation.counted

    def record_deferral(self, tax_year: int, amount: Decimal) -> None:
        """A loss of ``tax_year`` set aside after it was recorded (a purchase within the window
        after the sale): it leaves that year's gains until it is released."""
        if amount < ZERO:
            raise ValueError(f"a deferred amount is a magnitude, got {amount}")
        self._open_year(tax_year).gains += amount

    def record_income(self, event: IncomeEvent) -> None:
        year = self._open_year(event.received_on.year)
        year.income += event.gross
        year.withheld += event.withheld
        if event.source_country is not None and event.withheld > ZERO:
            year.foreign_income += event.gross

    # ── reading ──

    def years_with_activity(self) -> tuple[int, ...]:
        return tuple(sorted(year for year in self._years if year not in self._settled))

    def settled(self) -> tuple[TaxYearSummary, ...]:
        return tuple(self._settled[year] for year in sorted(self._settled))

    def carried_forward(self) -> tuple[CarryForward, ...]:
        return tuple(
            CarryForward(component=c.component, arising_year=c.arising_year, amount=c.amount)
            for c in self._carry
            if c.amount > ZERO
        )

    def provisional(self, tax_year: int) -> TaxYearSummary:
        """The summary ``tax_year`` would settle to now, without settling it."""
        return self._compute(tax_year, list(self._carry))[0]

    # ── settling ──

    def settle(self, tax_year: int) -> TaxYearSummary:
        """Close ``tax_year`` and return its summary. Earlier years with activity settle first."""
        if tax_year in self._settled:
            raise TaxError(f"tax year {tax_year} is already settled")
        earlier = [year for year in self.years_with_activity() if year < tax_year]
        if earlier:
            raise TaxError(f"tax year {earlier[0]} must be settled before {tax_year}")
        last = max(self._settled, default=None)
        if last is not None and tax_year < last:
            raise TaxError(f"tax year {tax_year} precedes the settled year {last}")
        summary, carry = self._compute(tax_year, self._carry)
        self._carry = carry
        self._settled[tax_year] = summary
        return summary

    def _compute(self, tax_year: int, carry: list[_Carry]) -> tuple[TaxYearSummary, list[_Carry]]:
        policy = self._policy
        year = self._years.get(tax_year, _Year())
        fraction = policy.cross_offset_fraction
        balances = _Balances(
            income=year.income,
            gains=year.gains,
            caps=_Caps(
                from_income=max(year.income, ZERO) * fraction,
                from_gains=max(year.gains, ZERO) * fraction,
            ),
        )
        _offset_within_year(balances)
        remaining = _use_carried(balances, carry, tax_year, policy.carry_forward_years)
        if balances.income < ZERO:
            remaining.append(_Carry("income", tax_year, -balances.income))
            balances.income = ZERO
        if balances.gains < ZERO:
            remaining.append(_Carry("gains", tax_year, -balances.gains))
            balances.gains = ZERO
        base = balances.income + balances.gains
        tax = policy.savings_tax(base)
        credit = policy.foreign_tax_credit(
            withheld=year.withheld, foreign_income=year.foreign_income, tax=tax, base=base
        )
        summary = TaxYearSummary(
            tax_year=tax_year,
            income=year.income,
            gains=year.gains,
            base=base,
            tax_before_credit=tax,
            foreign_tax_credit=credit,
            tax_due=tax - credit,
            carried_in_used=balances.used,
            carried_out=tuple(
                CarryForward(component=c.component, arising_year=c.arising_year, amount=c.amount)
                for c in remaining
                if c.amount > ZERO
            ),
            expired=balances.expired,
            rate_table_covers_year=policy.rate_table_covers(tax_year),
            payment_date=policy.payment_date(tax_year),
        )
        return summary, [c for c in remaining if c.amount > ZERO]


def _offset_within_year(balances: _Balances) -> None:
    """Step 1: one component's negative balance against the other's positive one, capped."""
    caps = balances.caps
    if balances.income < ZERO < balances.gains:
        use = min(-balances.income, caps.from_gains)
        balances.income += use
        balances.gains -= use
        caps.from_gains -= use
    if balances.gains < ZERO < balances.income:
        use = min(-balances.gains, caps.from_income)
        balances.gains += use
        balances.income -= use
        caps.from_income -= use


def _use_carried(
    balances: _Balances, carry: list[_Carry], tax_year: int, years: int
) -> list[_Carry]:
    """Step 2: earlier negative balances, oldest first; returns what is left of them."""
    left: list[_Carry] = []
    caps = balances.caps
    for item in sorted(carry, key=lambda c: c.arising_year):
        amount = item.amount
        if tax_year - item.arising_year > years:
            balances.expired += amount
            continue
        if item.component == "income":
            own = min(amount, max(balances.income, ZERO))
            balances.income -= own
            other = min(amount - own, caps.from_gains, max(balances.gains, ZERO))
            balances.gains -= other
            caps.from_gains -= other
        else:
            own = min(amount, max(balances.gains, ZERO))
            balances.gains -= own
            other = min(amount - own, caps.from_income, max(balances.income, ZERO))
            balances.income -= other
            caps.from_income -= other
        balances.used += own + other
        left.append(_Carry(item.component, item.arising_year, amount - own - other))
    return left
