"""The hunt beside the owner's bar (DECISIONS D185, D193, D195): the hunting share, the capital in
theses and in the reserve, the growth a year from the managed book's daily closes, after tax on
the broker's book and before tax on the shadow book's, and "bar, not forecast" beside it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from obliskiq.config import ObliskConfig
from obliskiq.domain.orders import ExecutionMode
from obliskiq.jurisdiction import TAX_POLICIES, load_profile
from obliskiq.reporting.bar import BAR_GROWTH, NOT_A_FORECAST, hunt_reading
from obliskiq.reporting.scoreboard import ASPIRATION
from obliskiq.store import Store
from obliskiq.store import breakers as breaker_repo
from obliskiq.store.breakers import Book, ManagedReading

D = Decimal
REPO = Path(__file__).resolve().parents[2]
TODAY = date(2026, 11, 20)


def close(store: Store, book: Book, day: date, managed: str) -> None:
    breaker_repo.record_reading(
        store,
        ManagedReading(
            book=book,
            as_of=day,
            managed=D(managed),
            reference_conid=None,
            reference_unit=None,
            is_close=True,
            recorded_at=datetime(day.year, day.month, day.day, 21, tzinfo=UTC),
        ),
    )


def test_the_bar_has_one_owner() -> None:
    assert ASPIRATION is BAR_GROWTH


def test_the_shadow_book_s_growth_is_before_tax_and_says_its_span(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    close(store, "shadow", TODAY - timedelta(days=365), "100000")
    close(store, "shadow", TODAY - timedelta(days=200), "104000")
    close(store, "shadow", TODAY, "110000")
    found = hunt_reading(store, shipped_settings, ExecutionMode.SHADOW, as_of=TODAY, policy=None)
    assert found.share == shipped_settings.thesis.capital_share
    assert (found.in_theses, found.in_reserve) == (D(0), D(110000))
    assert found.growth is not None
    assert abs(found.growth - D("0.10")) < D("0.001")
    assert (found.span_days, found.after_tax) == (365, False)
    assert found.note == "the shadow book keeps no tax lots"
    lines = found.lines()
    assert lines[0] == "hunting share 100%: 0.00 in theses, 110000.00 waiting in the reserve"
    assert "the record so far, 365 days (before tax)" in lines[1]
    assert NOT_A_FORECAST in lines[1]
    assert "the bar 50% a year over 3 years" in lines[1]


def test_a_later_close_is_not_read(store: Store, shipped_settings: ObliskConfig) -> None:
    close(store, "shadow", TODAY - timedelta(days=10), "100000")
    close(store, "shadow", TODAY, "100000")
    close(store, "shadow", TODAY + timedelta(days=1), "200000")
    found = hunt_reading(store, shipped_settings, ExecutionMode.SHADOW, as_of=TODAY, policy=None)
    assert found.growth == D(0)
    assert found.in_reserve == D(100000)


def test_without_two_closes_the_growth_is_not_measured(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    found = hunt_reading(store, shipped_settings, ExecutionMode.PAPER, as_of=TODAY, policy=None)
    assert (found.growth, found.in_reserve) == (None, None)
    assert found.lines() == [
        "hunting share 100%: 0.00 in theses, the reserve not measured yet",
        "growth beside the 50% bar: not measured (fewer than two daily closes of the managed book)",
    ]


def test_the_broker_s_book_is_after_tax_with_a_policy(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    policy = TAX_POLICIES.resolve(load_profile(REPO / "config", "es"), shipped_settings)
    close(store, "broker", date(2025, 12, 31), "100000")
    close(store, "broker", date(2026, 6, 30), "103000")
    close(store, "broker", TODAY, "105000")
    found = hunt_reading(store, shipped_settings, ExecutionMode.PAPER, as_of=TODAY, policy=policy)
    assert found.after_tax  # no lot realised a gain: nothing is taken
    assert found.year is not None
    assert abs(found.year - D("0.05")) < D("0.0001")
    assert "(after tax), this year +5.0%" in found.lines()[1]
    untaxed = hunt_reading(store, shipped_settings, ExecutionMode.PAPER, as_of=TODAY, policy=None)
    assert (untaxed.after_tax, untaxed.note) == (False, "no tax policy to price the lots")
