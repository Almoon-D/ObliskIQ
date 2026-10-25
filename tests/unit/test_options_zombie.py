"""The option side of the risk engine (lite's ``tests/test_compute_risk.py``, its option half,
ported): N(d2) at the target off the strike smile, greeks at the strike, the four conditions over
consecutive reviews, the penalty booked once at confirmation, the theta budget, and the sleeve's
date rules counted from the last tradable session.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.domain.enums import OptionRight
from obliskiq.domain.market import ChainRow
from obliskiq.domain.options import LedgerEventType, OptionPosition
from obliskiq.engines.options_sleeve.dates import SleeveDates
from obliskiq.engines.options_sleeve.penalty import PenaltyLedger
from obliskiq.engines.options_sleeve.zombie import (
    TARGET_DERIVED,
    TARGET_STATED,
    OptionRisk,
    ZombieError,
    ZombieTest,
)
from obliskiq.quant.smile import SOURCE_CLAMPED_HIGH, Smile, SmileFilters
from obliskiq.store import Store
from obliskiq.store import options as repo

OPENED = date(2026, 12, 1)
DAY1 = date(2027, 1, 5)
DAY2 = date(2027, 1, 6)
DAY3 = date(2027, 1, 7)
EXPIRY = date(2027, 2, 5)
TODAY = datetime(2027, 1, 20, 21, 0, tzinfo=UTC)


def smile(
    make_chain_row: Callable[..., ChainRow], settings: ObliskConfig, *, skew: float = 0.0
) -> Smile:
    """A smile whose implied vol falls with the strike when ``skew`` is positive (an equity
    skew), every strike inside the delta band."""
    points = (
        (90.0, -0.25, OptionRight.PUT),
        (100.0, 0.5, OptionRight.CALL),
        (110.0, 0.3, OptionRight.CALL),
        (120.0, 0.12, OptionRight.CALL),
        (130.0, 0.06, OptionRight.CALL),
    )
    rows = [
        make_chain_row(
            expiry=EXPIRY,
            strike=strike,
            delta=delta,
            right=right,
            iv=0.30 - skew * (strike - 100) / 100,
        )
        for strike, delta, right in points
    ]
    found = Smile.from_rows(rows, spot=100.0, filters=SmileFilters.from_config(settings))
    assert found is not None
    return found


@pytest.fixture
def engine(store: Store, shipped_settings: ObliskConfig) -> ZombieTest:
    return ZombieTest(store, shipped_settings, now=lambda: TODAY)


def open_position(store: Store, **overrides: Any) -> OptionPosition:
    fields: dict[str, Any] = {
        "position_id": None,
        "underlying_conid": 756733,
        "option_conid": None,
        "expiry": EXPIRY,
        "strike": 120.0,
        "right": OptionRight.CALL,
        "contracts": 2,
        "multiplier": 100.0,
        "premium_per_unit": Decimal("1.00"),
        "currency": "EUR",
        "premium_base": Decimal("200"),
        "fx_to_base": None,
        "opened_on": OPENED,
        "close_by": date(2027, 1, 22),
        "target": None,
        "prob_target_at_entry": 0.5,
        "catalyst_date": None,
        "intent_id": None,
    }
    fields.update(overrides)
    return repo.open_position(store, OptionPosition(**fields))


def zombie_risk(
    engine: ZombieTest, the_smile: Smile, *, as_of: date, bid: float | None = 0.2, **kw: Any
) -> OptionRisk:
    fields: dict[str, Any] = {
        "as_of": as_of,
        "spot": 100.0,
        "strike": 120.0,
        "is_call": True,
        "expiry": EXPIRY,
        "premium_per_unit": 1.0,
        "bid": bid,
        "smile": the_smile,
        "rate": 0.03,
        "dividend_yield": 0.0,
        "dividend_basis": "none collected",
        "prob_target_at_entry": 0.5,
    }
    fields.update(kw)
    return engine.assess(**fields)


# ── the reading ──


def test_the_greeks_read_the_strike_and_n_d2_the_target(
    shipped_settings: ObliskConfig, engine: ZombieTest, make_chain_row: Callable[..., ChainRow]
) -> None:
    """Two points of one smile: on an equity skew the target sits where implied vol is lower."""
    skewed = smile(make_chain_row, shipped_settings, skew=0.2)
    risk = zombie_risk(engine, skewed, as_of=DAY1, strike=110.0)
    assert risk.strike_vol.strike == 110.0
    assert risk.target_vol.strike == risk.target.level > 110.0
    assert risk.target_vol.iv < risk.strike_vol.iv
    assert risk.vol_source == shipped_settings.zombie_test.prob_vol_source


def test_a_clamped_read_says_so(
    shipped_settings: ObliskConfig, engine: ZombieTest, make_chain_row: Callable[..., ChainRow]
) -> None:
    risk = zombie_risk(
        engine, smile(make_chain_row, shipped_settings), as_of=DAY1, stated_target=150.0
    )
    assert risk.target_vol.source == SOURCE_CLAMPED_HIGH
    assert risk.target_vol.is_clamped


def test_a_stated_target_wins_and_a_derived_one_is_the_payoff_breakeven(
    shipped_settings: ObliskConfig, engine: ZombieTest
) -> None:
    ratio = shipped_settings.option_sleeve.min_payoff_ratio
    stated = engine.target_level(strike=100, premium_per_unit=2, is_call=True, stated=130.0)
    derived = engine.target_level(strike=100, premium_per_unit=2, is_call=True, stated=None)
    put = engine.target_level(strike=100, premium_per_unit=2, is_call=False, stated=None)
    assert (stated.level, stated.source) == (130.0, TARGET_STATED)
    assert (derived.level, derived.source) == (100 + ratio * 2, TARGET_DERIVED)
    assert put.level == 100 - ratio * 2


def test_a_payers_probability_and_delta_differ_from_the_zero_dividend_case(
    shipped_settings: ObliskConfig, engine: ZombieTest, make_chain_row: Callable[..., ChainRow]
) -> None:
    flat = smile(make_chain_row, shipped_settings)
    zero = zombie_risk(engine, flat, as_of=DAY1, strike=105.0)
    payer = zombie_risk(engine, flat, as_of=DAY1, strike=105.0, dividend_yield=0.05)
    assert payer.prob_target < zero.prob_target
    assert payer.delta < zero.delta


# ── the conditions ──


def test_a_position_with_plenty_of_tenor_is_not_a_zombie(
    shipped_settings: ObliskConfig, engine: ZombieTest, make_chain_row: Callable[..., ChainRow]
) -> None:
    risk = zombie_risk(
        engine,
        smile(make_chain_row, shipped_settings),
        as_of=DAY1,
        expiry=DAY1 + timedelta(days=150),
    )
    unmet = {c.name for c in engine.conditions(risk) if not c.met}
    assert "dte" in unmet


def test_a_drawdown_is_not_one_of_the_conditions(
    shipped_settings: ObliskConfig, engine: ZombieTest, make_chain_row: Callable[..., ChainRow]
) -> None:
    risk = zombie_risk(engine, smile(make_chain_row, shipped_settings), as_of=DAY1)
    assert {c.name for c in engine.conditions(risk)} == {
        "delta",
        "dte",
        "prob_target",
        "recoverable",
    }


def test_no_bid_fails_the_fourth_condition(
    shipped_settings: ObliskConfig, engine: ZombieTest, make_chain_row: Callable[..., ChainRow]
) -> None:
    risk = zombie_risk(engine, smile(make_chain_row, shipped_settings), as_of=DAY1, bid=None)
    (recoverable,) = [c for c in engine.conditions(risk) if c.name == "recoverable"]
    assert not recoverable.met
    assert "nothing to rescue" in recoverable.detail


def test_no_entry_probability_leaves_the_ratio_test_unrun(
    shipped_settings: ObliskConfig, engine: ZombieTest, make_chain_row: Callable[..., ChainRow]
) -> None:
    risk = zombie_risk(
        engine, smile(make_chain_row, shipped_settings), as_of=DAY1, prob_target_at_entry=None
    )
    (odds,) = [c for c in engine.conditions(risk) if c.name == "prob_target"]
    assert not odds.met
    assert "cannot run" in odds.detail


# ── the test over sessions ──


def test_a_single_session_never_fires(
    store: Store,
    shipped_settings: ObliskConfig,
    engine: ZombieTest,
    make_chain_row: Callable[..., ChainRow],
) -> None:
    held = open_position(store)
    first = engine.review(
        held, zombie_risk(engine, smile(make_chain_row, shipped_settings), as_of=DAY1)
    )
    assert first.all_conditions_met, first.summary()
    assert not first.fires
    assert "one session never triggers" in first.summary()


def test_two_consecutive_sessions_fire_and_book_the_penalty_once(
    store: Store,
    shipped_settings: ObliskConfig,
    engine: ZombieTest,
    make_chain_row: Callable[..., ChainRow],
) -> None:
    held = open_position(store)
    the_smile = smile(make_chain_row, shipped_settings)
    engine.review(held, zombie_risk(engine, the_smile, as_of=DAY1))
    second = engine.review(held, zombie_risk(engine, the_smile, as_of=DAY2))
    assert second.fires
    assert "confirmed on 2 consecutive" in second.summary()
    multiple = shipped_settings.option_sleeve.loss_penalty_multiple
    # 2 contracts x 100 x bid 0.20 recovered against a premium of 200: a loss of 160.
    assert second.penalty_booked == multiple * Decimal(160)
    third = engine.review(held, zombie_risk(engine, the_smile, as_of=DAY3))
    assert third.fires
    assert third.penalty_booked is None
    assert held.position_id is not None
    booked = repo.position_events(store, held.position_id, (LedgerEventType.LOSS_DETECTED,))
    assert len(booked) == 1, "one loss is never booked twice"


def test_a_clean_session_between_two_bad_ones_resets_the_streak(
    store: Store,
    shipped_settings: ObliskConfig,
    engine: ZombieTest,
    make_chain_row: Callable[..., ChainRow],
) -> None:
    held = open_position(store)
    the_smile = smile(make_chain_row, shipped_settings)
    engine.review(held, zombie_risk(engine, the_smile, as_of=DAY1))
    clean = engine.review(held, zombie_risk(engine, the_smile, as_of=DAY2, bid=0.01))
    assert clean.unmet() == ("recoverable",)
    assert clean.summary().startswith("not a zombie: the bid is")
    third = engine.review(held, zombie_risk(engine, the_smile, as_of=DAY3))
    assert third.consecutive_sessions == 1
    assert not third.fires


def test_a_dry_run_reports_the_streak_the_session_produces_and_writes_nothing(
    store: Store,
    shipped_settings: ObliskConfig,
    engine: ZombieTest,
    make_chain_row: Callable[..., ChainRow],
) -> None:
    held = open_position(store)
    the_smile = smile(make_chain_row, shipped_settings)
    engine.review(held, zombie_risk(engine, the_smile, as_of=DAY1))
    dry = engine.review(held, zombie_risk(engine, the_smile, as_of=DAY2), record=False)
    assert dry.fires
    assert dry.penalty_booked is None
    assert held.position_id is not None
    assert len(repo.observations(store, held.position_id, as_of=DAY3)) == 1


def test_re_reviewing_one_session_does_not_advance_the_streak(
    store: Store,
    shipped_settings: ObliskConfig,
    engine: ZombieTest,
    make_chain_row: Callable[..., ChainRow],
) -> None:
    held = open_position(store)
    the_smile = smile(make_chain_row, shipped_settings)
    engine.review(held, zombie_risk(engine, the_smile, as_of=DAY1))
    again = engine.review(held, zombie_risk(engine, the_smile, as_of=DAY1))
    assert again.consecutive_sessions == 1
    assert not again.fires


def test_a_review_of_a_future_session_or_before_the_position_is_refused(
    store: Store,
    shipped_settings: ObliskConfig,
    engine: ZombieTest,
    make_chain_row: Callable[..., ChainRow],
) -> None:
    held = open_position(store)
    the_smile = smile(make_chain_row, shipped_settings)
    with pytest.raises(ZombieError, match="has not happened"):
        engine.review(held, zombie_risk(engine, the_smile, as_of=TODAY.date() + timedelta(days=1)))
    with pytest.raises(ZombieError, match="before position"):
        engine.review(held, zombie_risk(engine, the_smile, as_of=OPENED - timedelta(days=1)))


def test_only_an_open_recorded_position_is_reviewed(
    store: Store,
    shipped_settings: ObliskConfig,
    engine: ZombieTest,
    make_chain_row: Callable[..., ChainRow],
) -> None:
    held = open_position(store)
    unrecorded = held.model_copy(update={"position_id": None})
    the_smile = smile(make_chain_row, shipped_settings)
    with pytest.raises(ZombieError):
        engine.review(unrecorded, zombie_risk(engine, the_smile, as_of=DAY1))


def test_booking_at_close_books_nothing_at_confirmation(
    store: Store, shipped_settings: ObliskConfig, make_chain_row: Callable[..., ChainRow]
) -> None:
    zombie = shipped_settings.zombie_test.model_copy(update={"book_penalty_at": "close"})
    settings = shipped_settings.model_copy(update={"zombie_test": zombie})
    engine = ZombieTest(store, settings, now=lambda: TODAY)
    held = open_position(store)
    the_smile = smile(make_chain_row, settings)
    engine.review(held, zombie_risk(engine, the_smile, as_of=DAY1))
    second = engine.review(held, zombie_risk(engine, the_smile, as_of=DAY2))
    assert second.fires
    assert second.penalty_booked is None
    assert PenaltyLedger(store, settings).balance(as_of=DAY2) == 0


def test_a_converted_premium_books_its_loss_in_the_base_currency(
    store: Store,
    shipped_settings: ObliskConfig,
    engine: ZombieTest,
    make_chain_row: Callable[..., ChainRow],
) -> None:
    held = open_position(
        store, currency="USD", premium_base=Decimal("171.20"), fx_to_base=Decimal("0.856")
    )
    the_smile = smile(make_chain_row, shipped_settings)
    engine.review(held, zombie_risk(engine, the_smile, as_of=DAY1))
    second = engine.review(held, zombie_risk(engine, the_smile, as_of=DAY2))
    multiple = shipped_settings.option_sleeve.loss_penalty_multiple
    recovered = Decimal("0.2") * 100 * 2 * Decimal("0.856")
    assert second.penalty_booked == multiple * (Decimal("171.20") - recovered)


# ── the theta budget ──


def test_the_theta_budget_fails_a_tenor_that_decays_below_the_floor(
    shipped_settings: ObliskConfig, engine: ZombieTest
) -> None:
    budget = engine.theta_budget(
        spot=100,
        strike=115,
        is_call=True,
        sigma=0.25,
        rate=0.03,
        dividend_yield=0.0,
        premium_per_unit=2.0,
        entry_dte=100,
        expected_resolution_days=95,
    )
    assert not budget.passes
    assert budget.floor == shipped_settings.zombie_test.min_recoverable_pct_of_premium


def test_the_theta_budget_passes_a_tenor_with_room(engine: ZombieTest) -> None:
    budget = engine.theta_budget(
        spot=100,
        strike=100,
        is_call=True,
        sigma=0.25,
        rate=0.03,
        dividend_yield=0.0,
        premium_per_unit=5.0,
        entry_dte=180,
        expected_resolution_days=60,
    )
    assert budget.passes
    assert budget.resolution_dte == 120


def test_the_theta_budget_takes_the_dividend_yield(engine: ZombieTest) -> None:
    kwargs: dict[str, Any] = {
        "spot": 100,
        "strike": 100,
        "is_call": True,
        "sigma": 0.25,
        "rate": 0.03,
        "premium_per_unit": 5.0,
        "entry_dte": 180,
        "expected_resolution_days": 60,
    }
    payer = engine.theta_budget(dividend_yield=0.05, **kwargs)
    zero = engine.theta_budget(dividend_yield=0.0, **kwargs)
    assert payer.value_at_resolution < zero.value_at_resolution


# ── the dates ──


def test_the_sell_by_session_counts_from_the_last_tradable_session(
    shipped_settings: ObliskConfig, calendars: Calendars
) -> None:
    """A Monday expiry after a Friday holiday lands on the Thursday before (Good Friday 2027 is
    26 March; Easter Monday is no NYSE holiday, so a holiday expiry is used instead)."""
    dates = SleeveDates(shipped_settings, calendars)
    found = dates.close_by(
        option_exchange="CBOE", underlying_exchange="ARCA", expiry=date(2027, 3, 26)
    )
    assert found.last_tradable == date(2027, 3, 25)
    assert found.session is not None
    sessions = shipped_settings.tenor.mandatory_close_trading_days
    assert calendars.session_before("XNYS", "XNYS", date(2027, 3, 25), sessions) == found.session


def test_an_exchange_the_calendars_cannot_place_is_no_date(
    shipped_settings: ObliskConfig, calendars: Calendars
) -> None:
    found = SleeveDates(shipped_settings, calendars).close_by(
        option_exchange="SMART", underlying_exchange="ARCA", expiry=date(2027, 3, 19)
    )
    assert found.session is None
    assert "SMART" in found.reason


def test_the_catalyst_sets_the_window(shipped_settings: ObliskConfig, calendars: Calendars) -> None:
    tenor = shipped_settings.tenor
    dates = SleeveDates(shipped_settings, calendars)
    undated = dates.window(as_of=DAY1, catalyst_date=None)
    assert undated.earliest == DAY1 + timedelta(days=tenor.entry_dte_min)
    assert undated.latest == DAY1 + timedelta(days=tenor.entry_dte_max)
    catalyst = DAY1 + timedelta(days=150)
    dated = dates.window(as_of=DAY1, catalyst_date=catalyst)
    assert dated.earliest == catalyst + timedelta(days=tenor.catalyst_buffer_days)
    assert dated.admits(dated.earliest)
    assert not dated.admits(dated.earliest - timedelta(days=1))
    too_far = dates.window(as_of=DAY1, catalyst_date=DAY1 + timedelta(days=300))
    assert too_far.is_empty
