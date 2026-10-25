"""Month-end closes in the base currency: the research panel and the stored bars of the account are
read by one function and agree; a gap, a stale close or a missing rate ends the reading and says
why.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import dataclasses
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sim_rig import instrument, series, settings

from obliskiq.config import ObliskConfig
from obliskiq.decision.core_closes import StoredCloses, last_month_end
from obliskiq.engines.core.closes import month_end, month_end_closes
from obliskiq.errors import TrendUnavailableError
from obliskiq.research.simulate import Panel, PanelIndex, Pricing

D = Decimal


def sessions(first: date, last: date, level: float = 100.0) -> list[tuple[date, float]]:
    """A close every weekday from ``first`` to ``last``, rising a little each day."""
    out, day, k = [], first, 0
    while day <= last:
        if day.weekday() < 5:
            out.append((day, level + 0.1 * k))
            k += 1
        day += timedelta(days=1)
    return out


def keep(_day: date, value: Decimal) -> Decimal:
    return value


def test_a_month_ends_close_is_the_latest_session_on_or_before_it() -> None:
    daily = sessions(date(2026, 1, 5), date(2026, 4, 30))
    got = month_end_closes(daily, keep, through=date(2026, 3, 31), max_age_days=7, name="X")
    assert [day for day, _ in got] == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]
    by_day = dict(daily)
    # 31 January 2026 is a Saturday: the close of Friday the 30th.
    assert got[0][1] == by_day[date(2026, 1, 30)]
    assert got[2][1] == by_day[date(2026, 3, 31)]
    assert month_end(2024, 2) == date(2024, 2, 29)


def test_no_history_has_no_closes() -> None:
    assert month_end_closes([], keep, through=date(2026, 3, 31), max_age_days=7, name="X") == ()


def test_a_month_without_a_fresh_close_ends_the_reading() -> None:
    daily = [d for d in sessions(date(2026, 1, 5), date(2026, 4, 30)) if d[0].month != 2]
    with pytest.raises(TrendUnavailableError, match="X has no month-end close on 2026-02-28"):
        month_end_closes(daily, keep, through=date(2026, 3, 31), max_age_days=7, name="X")
    # A close too old is stale, a close within the age is not.
    sparse = [(date(2026, 1, 15), 100.0), (date(2026, 2, 20), 101.0)]
    with pytest.raises(TrendUnavailableError, match="2026-01-31"):
        month_end_closes(sparse, keep, through=date(2026, 2, 28), max_age_days=7, name="X")
    assert month_end_closes(sparse, keep, through=date(2026, 2, 28), max_age_days=16, name="X")


def test_a_close_that_cannot_be_converted_ends_the_reading() -> None:
    daily = sessions(date(2026, 1, 5), date(2026, 2, 27))
    with pytest.raises(TrendUnavailableError, match="no month-end close on 2026-01-31"):
        month_end_closes(
            daily, lambda _d, _v: None, through=date(2026, 2, 28), max_age_days=7, name="X"
        )
    with pytest.raises(TrendUnavailableError, match="no month-end close on 2026-01-31"):
        month_end_closes(
            daily, lambda _d, _v: D(0), through=date(2026, 2, 28), max_age_days=7, name="X"
        )


def test_the_last_completed_month_end_is_the_one_before_this_month() -> None:
    assert last_month_end(date(2026, 10, 26)) == date(2026, 9, 30)
    assert last_month_end(date(2026, 1, 1)) == date(2025, 12, 31)
    assert last_month_end(date(2026, 3, 31)) == date(2026, 2, 28)


@pytest.mark.parametrize("currency", ["EUR", "USD"])
def test_the_panels_closes_and_the_accounts_are_the_same(currency: str) -> None:
    """The research panel's reading (``Pricing``) and the account's (``month_end_closes``) of one
    history, in the euro and in a currency converted at the day's reference rate."""
    first, last = date(2025, 1, 6), date(2026, 9, 30)
    daily = sessions(first, last)
    rates = [(day, 1.1 + 0.0005 * k) for k, (day, _) in enumerate(daily)]
    held = dataclasses.replace(instrument("X", "I"), currency=currency)
    panel = Panel(prices={"X": series(daily)}, income={}, fx={"USD": series(rates)})
    pricing = Pricing({"X": held}, PanelIndex(panel), settings("1000"))
    theirs = pricing.month_end_closes("X", last)
    by_day = dict(rates)

    def convert(day: date, value: Decimal) -> Decimal | None:
        if currency == "EUR":
            return value
        return value * D(1) / D(repr(by_day[day]))

    ours = month_end_closes(daily, convert, through=last, max_age_days=7, name="X")
    assert ours == theirs
    assert len(ours) == 21


# ── the account's bars ──


class FakeBar:
    def __init__(self, session: date, close: float) -> None:
        self.session_date, self.close = session, close


class FakeWorker:
    """Hands back the bars it was given for the conid asked about."""

    def __init__(self, bars: dict[int, list[tuple[date, float]]]) -> None:
        self.bars = bars

    async def run(self, _function: Any, conid: int, **_kw: Any) -> list[FakeBar]:
        return [FakeBar(d, c) for d, c in self.bars.get(conid, [])]


class FakeDirectory:
    def __init__(self, currencies: dict[int, str]) -> None:
        self.currencies = currencies

    def describe(self, conid: int) -> Any:
        currency = self.currencies.get(conid)
        if currency is None:
            return None
        return type("Described", (), {"currency": currency, "symbol": f"S{conid}"})()


class FakeRates:
    def __init__(self, rate: Decimal | None) -> None:
        self.rate = rate
        self.loaded: list[date] = []

    async def load(self, as_of: date) -> None:
        self.loaded.append(as_of)

    def to_base(self, amount: Decimal, _currency: str, _on: date) -> Decimal | None:
        return None if self.rate is None else amount / self.rate


async def test_stored_bars_become_month_ends_and_a_gap_is_kept_with_its_reason(
    shipped_settings: ObliskConfig,
) -> None:
    through = date(2026, 9, 30)
    good = sessions(date(2025, 6, 2), through)
    holey = [d for d in good if d[0].month != 12 or d[0].year != 2025]
    worker = FakeWorker({1: good, 2: good, 3: holey, 4: good, 5: []})
    directory = FakeDirectory({1: "EUR", 2: "USD", 3: "EUR", 5: "EUR"})
    rates = FakeRates(D("2"))
    stored = StoredCloses(
        shipped_settings,
        worker=worker,  # type: ignore[arg-type]
        directory=directory,  # type: ignore[arg-type]
        rates=rates,
    )
    view = await stored.view({"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}, through=through)
    assert view.as_of == through
    assert set(view.closes) == {"a", "b"}
    assert dict(view.closes["b"])[through] == pytest.approx(dict(good)[through] / 2)
    assert "S3 (3) has no month-end close on 2025-12-31" in view.gaps["c"]
    assert view.gaps["d"] == "4 is not described, so its currency is unknown"
    assert view.gaps["e"] == "S5 (5) has no stored daily closes"
    assert rates.loaded == [through]


async def test_a_foreign_close_without_a_rate_history_is_a_gap(
    shipped_settings: ObliskConfig,
) -> None:
    through = date(2026, 9, 30)
    bars = sessions(date(2025, 6, 2), through)
    for rates in (None, FakeRates(None)):
        stored = StoredCloses(
            shipped_settings,
            worker=FakeWorker({2: bars}),  # type: ignore[arg-type]
            directory=FakeDirectory({2: "USD"}),  # type: ignore[arg-type]
            rates=rates,
        )
        view = await stored.view({"b": 2}, through=through)
        assert "b" not in view.closes
        assert "has no month-end close" in view.gaps["b"]
