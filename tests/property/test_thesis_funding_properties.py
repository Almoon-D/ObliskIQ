"""Properties of the reserve's funding sale (DECISIONS D195): a sale never takes more units than
are held, the sales raise the need net of their own costs or nothing is sold, and so a thesis
funded by them and then entered never takes the cash below its floor, in any currency, at any
step.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from obliskiq.capital.planner import spread_funding

D = Decimal
ZERO = D(0)


@dataclass(frozen=True)
class Held:
    conid: int
    symbol: str
    venue: str
    units: Decimal
    unit: Decimal


def cost(commission: Decimal, bps: Decimal):  # type: ignore[no-untyped-def]
    """A sale's cost: a fixed commission and a share of its proceeds."""

    def priced(held: Held, units: Decimal) -> Decimal:
        return commission + units * held.unit * bps / 10000

    return priced


holdings = st.lists(
    st.tuples(
        st.integers(min_value=1, max_value=5000),
        st.decimals(min_value="0.5", max_value="900", places=2),
    ),
    min_size=1,
    max_size=6,
).map(
    lambda rows: [
        Held(conid=n + 1, symbol=f"H{n}", venue="XAMS", units=D(units), unit=unit)
        for n, (units, unit) in enumerate(rows)
    ]
)
amounts = st.decimals(min_value="0", max_value="200000", places=2)


@settings(max_examples=300, deadline=None)
@given(
    holdings,
    amounts,
    st.decimals(min_value="0", max_value="20", places=2),
    st.decimals(min_value="0", max_value="50", places=1),
)
def test_a_funding_sale_raises_its_need_or_sells_nothing(
    held: list[Held], need: Decimal, commission: Decimal, bps: Decimal
) -> None:
    sales = spread_funding(held, need, cost(commission, bps))
    if isinstance(sales, str):
        return
    by_conid = {h.conid: h for h in held}
    for sale in sales:
        assert ZERO <= sale.units <= by_conid[sale.conid].units
        assert sale.units == sale.units.to_integral_value()
        assert sale.proceeds == sale.units * by_conid[sale.conid].unit
    assert sum((s.proceeds - s.cost for s in sales), ZERO) >= need


@settings(max_examples=300, deadline=None)
@given(
    holdings,
    amounts,
    amounts,
    st.decimals(min_value="0", max_value="5000", places=2),
    st.decimals(min_value="0", max_value="20", places=2),
)
def test_a_funded_entry_never_takes_the_cash_below_its_floor(
    held: list[Held], cash: Decimal, size: Decimal, floor: Decimal, commission: Decimal
) -> None:
    # One currency's ledger through the cycle: the reserve's sales fill, then the entry.
    spendable = max(ZERO, cash - floor)
    steps = [cash]
    if size > spendable:
        sales = spread_funding(held, size - spendable, cost(commission, D(5)))
        if isinstance(sales, str):
            return  # nothing is sold, and the thesis waits: the cash is untouched
        steps.append(steps[-1] + sum((s.proceeds - s.cost for s in sales), ZERO))
    steps.append(steps[-1] - size)
    assert all(step >= ZERO for step in steps)
    assert steps[-1] >= min(cash, floor)
