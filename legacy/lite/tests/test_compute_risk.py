"""The risk engine: the zombie test, stops, caps, the breaker, averaging down.

Most of these assert a refusal. The engine's job is to stop the account doing
the things it has already done — capitulating on a drawdown, setting a stop
inside the noise, averaging down into a falling price — so what it will not do
matters more than what it will.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from obliskiq.compute.frames import BarSeries, ChainRow
from obliskiq.compute.penalty import PenaltyLedger
from obliskiq.compute.risk_engine import (
    CAP_COUNTRY,
    CAP_SECTOR,
    TARGET_DERIVED,
    TARGET_FROM_LEDGER,
    UNRELIABLE,
    Holding,
    RiskEngine,
)
from obliskiq.compute.smile import Smile
from obliskiq.store.ledger import (
    LedgerError,
    append_reunderwrite,
    get_position,
    open_position,
)

AS_OF = date(2026, 3, 2)
EXPIRY = date(2026, 4, 10)
#: The zero-dividend case, named. It used to be the silent default inside the
#: engine; these fixtures state it so a test reading N(d2) knows which case it
#: is reading, and so the payer tests below have something to differ from.
NO_DIVIDEND = 0.0

RATE = 0.03


@pytest.fixture()
def engine(seeded_store, config):
    return RiskEngine(seeded_store, config)


@pytest.fixture()
def position_id(seeded_store, config):
    return open_position(
        seeded_store,
        contract_id=1,
        opened_at="2025-11-01",
        entry_price=3.00,
        thesis_text="implied vol in the bottom decile into a dated ruling",
        invalidation_trigger="spot closes below 88.00",
        prob_target_at_entry=0.30,
    )


def chain_row(strike, right, iv):
    return ChainRow(
        snapshot_date=AS_OF,
        underlying_id=1,
        expiry=EXPIRY,
        strike=strike,
        right=right,
        bid=0.4,
        ask=0.5,
        last=0.45,
        iv=iv,
        delta=None,
        open_interest=2_000,
        volume=30,
        contract_id=None,
    )


def dead_smile():
    """A surface around a spot of 90, well below a strike of 120."""
    return Smile.from_rows(
        [
            chain_row(80.0, "P", 0.46),
            chain_row(90.0, "P", 0.40),
            chain_row(100.0, "C", 0.36),
            chain_row(120.0, "C", 0.42),
            chain_row(140.0, "C", 0.50),
        ],
        spot=90.0,
    )


def dead_option(engine, *, bid=0.60, premium=3.00, prob_at_entry=0.30):
    """A position matching all four zombie conditions: low delta, near expiry."""
    return engine.assess_option(
        as_of=AS_OF,
        spot=90.0,
        strike=120.0,
        is_call=True,
        expiry=EXPIRY,
        premium_paid=premium,
        current_bid=bid,
        smile=dead_smile(),
        rate=RATE,
        dividend_yield=NO_DIVIDEND,
        prob_target_at_entry=prob_at_entry,
        exchange="ARCA",
    )


# ── two reads off one smile, at two different points ──


def test_the_greeks_use_the_strikes_own_vol_and_n_d2_the_targets(engine):
    """They priced two different things off ONE volatility, and it biased delta.

    N(d2) asks how likely the underlying is to reach the TARGET, so it takes the
    smile at the target. The greeks price THIS contract, so they take the smile
    at its own strike. Feeding the target's vol to the greeks understated delta
    on a normal call skew — the target sits further out than the strike, where
    implied vol is lower — and that delta is zombie condition 1.
    """
    risk = dead_option(engine)
    smile = dead_smile()

    at_strike = smile.iv_at(risk.strike)
    at_target = smile.iv_at(risk.target.level)
    assert at_strike is not None and at_target is not None
    assert at_strike.iv != pytest.approx(at_target.iv), (
        "the fixture smile has no skew between these two points, so this test "
        "could not tell the two reads apart"
    )

    assert risk.vol_used == pytest.approx(at_target.iv), "N(d2) reads at the target"
    assert risk.strike_vol == pytest.approx(at_strike.iv), "the greeks read at the strike"

    from obliskiq.quant.bsm import bsm_greeks

    on_strike_vol = bsm_greeks(
        risk.spot,
        risk.strike,
        risk.dte,
        at_strike.iv,
        RATE,
        NO_DIVIDEND,
        risk.is_call,
        # The same numerics object the engine used, so this is the identical
        # computation with one input changed.
        engine._bsm,
    )
    assert risk.delta == pytest.approx(float(on_strike_vol.delta))


def test_a_clamped_smile_read_says_so_rather_than_passing_as_measured(engine):
    """A 3:1 target is far out, and often past the strikes that survived hygiene.

    The read is then the outermost surviving wing rather than a measurement at
    the level asked about. That is still the best answer available — it is not
    silently the same thing, and SmileQuote records which it was.
    """
    narrow = Smile.from_rows(
        [
            chain_row(80.0, "P", 0.46),
            chain_row(90.0, "P", 0.40),
            chain_row(100.0, "C", 0.36),
            chain_row(120.0, "C", 0.42),
        ],
        spot=90.0,
    )
    risk = engine.assess_option(
        as_of=AS_OF,
        spot=90.0,
        strike=120.0,
        is_call=True,
        expiry=EXPIRY,
        premium_paid=3.00,
        current_bid=0.60,
        smile=narrow,
        rate=RATE,
        dividend_yield=NO_DIVIDEND,
        prob_target_at_entry=0.30,
        exchange="ARCA",
    )

    assert risk.target.level > 120.0, "the target has to sit past the last strike"
    assert risk.vol_quote.is_clamped, "a target beyond the collected strikes clamps"
    assert not risk.vol_quote.is_measured
    assert "clamped" in risk.vol_quote.provenance()

    # The strike itself IS listed, so that read is a measurement. The two reads
    # coincide in value here and still differ in what they are.
    assert risk.strike_vol_quote is not None
    assert risk.strike_vol_quote.is_measured


# ── the target and the probability ──


def test_a_stated_target_wins_over_a_derived_one(config, engine):
    stated = engine.target_level(strike=100.0, premium_paid=2.0, is_call=True, stated=118.0)
    assert stated.level == 118.0
    assert stated.source == TARGET_FROM_LEDGER
    assert stated.is_stated


def test_a_derived_target_is_the_payoff_ratio_breakeven(config, engine):
    ratio = config.require_float("option_sleeve.min_payoff_ratio")
    derived = engine.target_level(strike=100.0, premium_paid=2.0, is_call=True, stated=None)
    assert derived.level == pytest.approx(100.0 + ratio * 2.0)
    assert derived.source == TARGET_DERIVED
    assert not derived.is_stated


def test_a_put_target_is_below_the_strike(config, engine):
    ratio = config.require_float("option_sleeve.min_payoff_ratio")
    derived = engine.target_level(strike=100.0, premium_paid=2.0, is_call=False, stated=None)
    assert derived.level == pytest.approx(100.0 - ratio * 2.0)


def test_the_probability_uses_the_smile_at_the_target_not_atm(config, engine):
    """zombie_test.prob_vol_source is strike_smile_iv, and this proves it bites."""
    assert engine.prob_vol_source == "strike_smile_iv"
    smile = dead_smile()
    risk = dead_option(engine)

    target_iv = smile.iv_at(risk.target.level).iv
    atm_iv = smile.atm().iv

    assert risk.vol_used == pytest.approx(target_iv)
    assert risk.vol_used != pytest.approx(atm_iv), (
        "this fixture exists to have a skew; if the two coincide the test proves nothing"
    )


# ── the zombie test ──


def test_a_single_session_never_fires(seeded_store, engine, position_id):
    """The headline rule. One session cannot trigger an exit."""
    risk = dead_option(engine)
    assessment = engine.zombie(risk, position_id=position_id)

    assert assessment.all_conditions_met, "the fixture is meant to meet all four"
    assert not assessment.fires
    assert assessment.consecutive_sessions == 1
    assert "single session" in assessment.summary()


def test_two_consecutive_sessions_fire(config, seeded_store, engine, position_id):
    required = config.require_int("zombie_test.consecutive_sessions_required")
    assert required == 2, "this test is written for a two-session requirement"

    first = engine.zombie(dead_option(engine), position_id=position_id)
    assert not first.fires

    later = engine.assess_option(
        as_of=AS_OF + timedelta(days=1),
        spot=90.0,
        strike=120.0,
        is_call=True,
        expiry=EXPIRY,
        premium_paid=3.00,
        current_bid=0.60,
        smile=dead_smile(),
        rate=RATE,
        dividend_yield=NO_DIVIDEND,
        prob_target_at_entry=0.30,
        exchange="ARCA",
    )
    second = engine.zombie(later, position_id=position_id)

    assert second.fires
    assert second.consecutive_sessions == required


def test_a_review_dated_in_the_future_is_refused(engine, position_id):
    """The confirmation counts distinct observation dates, and the date is the
    caller's ``as_of``. Nothing bounded it, so a review could be recorded for a
    session that has not happened — and two such calls reach the count and book a
    penalty that cannot be reversed.
    """
    from obliskiq.util.timeutil import utc_today

    ahead = engine.assess_option(
        as_of=utc_today() + timedelta(days=1),
        spot=90.0,
        strike=120.0,
        is_call=True,
        expiry=EXPIRY,
        premium_paid=3.00,
        current_bid=0.60,
        smile=dead_smile(),
        rate=RATE,
        dividend_yield=NO_DIVIDEND,
        prob_target_at_entry=0.30,
        exchange="ARCA",
    )
    with pytest.raises(LedgerError, match="future"):
        engine.zombie(ahead, position_id=position_id)


def test_a_review_dated_before_the_position_existed_is_refused(engine, position_id):
    before = engine.assess_option(
        as_of=date(2025, 10, 1),  # the position opened 2025-11-01
        spot=90.0,
        strike=120.0,
        is_call=True,
        expiry=EXPIRY,
        premium_paid=3.00,
        current_bid=0.60,
        smile=dead_smile(),
        rate=RATE,
        dividend_yield=NO_DIVIDEND,
        prob_target_at_entry=0.30,
        exchange="ARCA",
    )
    with pytest.raises(LedgerError, match="before position"):
        engine.zombie(before, position_id=position_id)


def test_a_clean_session_between_two_bad_ones_resets_the_streak(seeded_store, engine, position_id):
    engine.zombie(dead_option(engine), position_id=position_id)

    alive = engine.assess_option(
        as_of=AS_OF + timedelta(days=1),
        spot=118.0,  # the underlying came back; delta is no longer negligible
        strike=120.0,
        is_call=True,
        expiry=EXPIRY,
        premium_paid=3.00,
        current_bid=2.80,
        smile=Smile.from_rows(
            [chain_row(110.0, "P", 0.34), chain_row(120.0, "C", 0.33), chain_row(130.0, "C", 0.36)],
            spot=118.0,
        ),
        rate=RATE,
        dividend_yield=NO_DIVIDEND,
        prob_target_at_entry=0.30,
        exchange="ARCA",
    )
    recovered = engine.zombie(alive, position_id=position_id)
    assert not recovered.all_conditions_met
    assert recovered.consecutive_sessions == 0


def test_a_drawdown_is_not_one_of_the_conditions(engine, position_id):
    """Percentage drawdown is explicitly NOT a trigger, so it is not an input."""
    assessment = engine.zombie(dead_option(engine), position_id=position_id)
    names = {condition.name for condition in assessment.conditions}
    assert names == {"delta", "dte", "prob_target", "recoverable"}
    assert not any("drawdown" in name for name in names)


def test_a_position_with_plenty_of_tenor_is_not_a_zombie(engine, position_id):
    """Down on the mark with months left is noise, not a thesis failure."""
    far = engine.assess_option(
        as_of=AS_OF,
        spot=90.0,
        strike=120.0,
        is_call=True,
        expiry=AS_OF + timedelta(days=180),
        premium_paid=3.00,
        current_bid=0.90,  # down 70% and still not a zombie
        smile=dead_smile(),
        rate=RATE,
        dividend_yield=NO_DIVIDEND,
        prob_target_at_entry=0.30,
        exchange="ARCA",
    )
    assessment = engine.zombie(far, position_id=position_id)
    assert not assessment.all_conditions_met
    assert "dte" in assessment.unmet()


def test_a_negligible_bid_fails_the_fourth_condition(engine, position_id):
    """Conditions 1-3 hold, but closing achieves nothing after commission."""
    assessment = engine.zombie(dead_option(engine, bid=0.01), position_id=position_id)
    assert not assessment.all_conditions_met
    assert assessment.unmet() == ("recoverable",)


def test_confirmation_books_the_penalty_at_detection(config, seeded_store, engine, position_id):
    """Booked when the decision lands, not when the trade is done."""
    multiple = config.require_float("option_sleeve.loss_penalty_multiple")
    ledger = PenaltyLedger(seeded_store, config)
    assert ledger.balance() == 0.0

    engine.zombie(dead_option(engine), position_id=position_id)
    assert ledger.balance() == 0.0, "one session must not book anything"

    later = engine.assess_option(
        as_of=AS_OF + timedelta(days=1),
        spot=90.0,
        strike=120.0,
        is_call=True,
        expiry=EXPIRY,
        premium_paid=3.00,
        current_bid=0.60,
        smile=dead_smile(),
        rate=RATE,
        dividend_yield=NO_DIVIDEND,
        prob_target_at_entry=0.30,
        exchange="ARCA",
    )
    fired = engine.zombie(later, position_id=position_id)

    assert fired.fires
    assert fired.penalty_booked == pytest.approx(multiple * (3.00 - 0.60))
    assert ledger.balance() == pytest.approx(multiple * 2.40)


def test_a_third_confirming_session_does_not_book_the_penalty_twice(
    seeded_store, engine, position_id, config
):
    ledger = PenaltyLedger(seeded_store, config)
    for offset in range(3):
        engine.zombie(
            engine.assess_option(
                as_of=AS_OF + timedelta(days=offset),
                spot=90.0,
                strike=120.0,
                is_call=True,
                expiry=EXPIRY,
                premium_paid=3.00,
                current_bid=0.60,
                smile=dead_smile(),
                rate=RATE,
                dividend_yield=NO_DIVIDEND,
                prob_target_at_entry=0.30,
                exchange="ARCA",
            ),
            position_id=position_id,
        )
    multiple = config.require_float("option_sleeve.loss_penalty_multiple")
    assert ledger.balance() == pytest.approx(multiple * 2.40)


# ── tenor and theta ──


def test_the_mandatory_close_date_comes_back_with_its_calendar(config, engine):
    days = config.require_int("tenor.mandatory_close_trading_days")
    close = engine.mandatory_close(date(2026, 9, 18), exchange="ARCA")
    assert close.trading_days_before == days
    assert close.calendar_id == "XNYS"
    assert close.close_date == date(2026, 9, 3)


def test_a_futures_option_gets_the_wider_close_buffer(config, engine):
    """Overnight gap risk is worse, so the exit needs more room."""
    equity_days = config.require_int("tenor.mandatory_close_trading_days")
    fop_days = config.require_int("instruments.futures_options_mandatory_close_trading_days")
    assert fop_days > equity_days

    equity = engine.mandatory_close(date(2026, 9, 18), exchange="ARCA")
    fop = engine.mandatory_close(date(2026, 9, 18), exchange="ARCA", is_futures_option=True)
    assert fop.close_date < equity.close_date


def test_the_theta_budget_fails_a_tenor_that_decays_below_the_floor(config, engine):
    floor = config.require_float("zombie_test.min_recoverable_pct_of_premium")

    # An out-of-the-money call held nearly to expiry with nothing else moving.
    budget = engine.theta_budget(
        spot=100.0,
        strike=130.0,
        is_call=True,
        sigma=0.30,
        rate=RATE,
        dividend_yield=NO_DIVIDEND,
        premium_paid=2.00,
        entry_dte=180,
        expected_resolution_days=170,
    )
    assert budget.remaining_pct_of_premium < floor
    assert not budget.passes
    assert "FAILS" in budget.summary()


def test_the_theta_budget_passes_a_tenor_with_room(engine):
    budget = engine.theta_budget(
        spot=100.0,
        strike=105.0,
        is_call=True,
        sigma=0.30,
        rate=RATE,
        dividend_yield=NO_DIVIDEND,
        premium_paid=6.00,
        entry_dte=180,
        expected_resolution_days=60,
    )
    assert budget.passes


# ── equity stops ──


def _bars(*, span: float, closes: int = 40, base: float = 100.0) -> BarSeries:
    series = BarSeries(contract_id=1, freq="1d")
    for index in range(closes):
        close = base + index * 0.1
        series.ts.append(f"2026-01-{(index % 28) + 1:02d}T00:00:00Z")
        series.open.append(close)
        series.high.append(close + span / 2)
        series.low.append(close - span / 2)
        series.close.append(close)
        series.volume.append(1000.0)
    return series


def test_the_stop_widens_for_a_high_atr_name(config, engine):
    """A 20% stop is noise on a 90%-vol name and far too loose on a 15%-vol one."""
    quiet = engine.equity_stops(_bars(span=1.0), entry_price=100.0)
    loud = engine.equity_stops(_bars(span=8.0), entry_price=100.0)

    assert quiet is not None and loud is not None
    assert loud.atr > quiet.atr
    assert loud.stop_distance_pct > quiet.stop_distance_pct
    assert loud.stop < quiet.stop


def test_the_stop_is_the_configured_atr_multiple_and_nothing_else(config, engine):
    multiple = config.require_float("equity_risk.stop_atr_multiple")
    stops = engine.equity_stops(_bars(span=2.0), entry_price=100.0)
    assert stops.stop == pytest.approx(100.0 - multiple * stops.atr)
    assert stops.atr_period == config.require_int("data.atr_period")


def test_no_two_names_share_a_stop_distance_by_percentage(engine):
    """A fixed-percentage stop would give both the same distance. None is produced."""
    distances = {
        round(engine.equity_stops(_bars(span=span), entry_price=100.0).stop_distance_pct, 6)
        for span in (1.0, 3.0, 6.0)
    }
    assert len(distances) == 3


def test_a_bar_with_no_high_does_not_take_down_the_whole_tool_call():
    """A regression, found while adding the range estimators.

    ``bar_series`` drops a row with no CLOSE and keeps one with no high — a bar
    with a close still carries a return. So ``high`` holds ``None`` in real
    stores, and both check_position_risk and get_portfolio_review reached the
    trailing stop through a bare ``max(bars.high)``, which raises TypeError in
    Python 3 rather than returning anything. One NULL high on one instrument
    failed the entire tool call, for every position in it.

    ``stats.true_ranges`` beside it has always guarded. This is that guard,
    written once on BarSeries so the two callers cannot diverge again.
    """
    from obliskiq.compute.frames import BarSeries

    series = BarSeries(
        contract_id=1,
        freq="1d",
        ts=["2026-01-01", "2026-01-02", "2026-01-03"],
        high=[101.0, None, 105.0],  # type: ignore[list-item]
        low=[99.0, None, 103.0],  # type: ignore[list-item]
        close=[100.0, 102.0, 104.0],
    )
    assert series.trailing_high == 105.0

    with pytest.raises(TypeError):
        max(series.high)  # what it used to do, kept so the fix cannot be undone

    assert BarSeries(contract_id=1, freq="1d").trailing_high is None
    assert (
        BarSeries(contract_id=1, freq="1d", high=[None, None], close=[1.0, 2.0]).trailing_high  # type: ignore[list-item]
        is None
    ), "no usable high is None, never a stop computed off nothing"


def test_the_trailing_stop_follows_the_running_high(config, engine):
    trailing_multiple = config.require_float("equity_risk.trailing_stop_atr_multiple")
    stops = engine.equity_stops(_bars(span=2.0), entry_price=100.0, trailing_high=140.0)
    assert stops.trailing_stop == pytest.approx(140.0 - trailing_multiple * stops.atr)
    assert stops.effective_stop == stops.trailing_stop


def test_a_fixed_percentage_stop_type_is_refused_outright(config, seeded_store):
    import copy

    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["equity_risk"]["stop_type"] = "fixed_pct"
    engine = RiskEngine(seeded_store, Config(data, config.source))

    with pytest.raises(ValueError, match="volatility-scaled"):
        engine.equity_stops(_bars(span=2.0), entry_price=100.0)


def test_short_history_gives_no_stop_rather_than_a_guessed_one(engine):
    assert engine.equity_stops(_bars(span=2.0, closes=3), entry_price=100.0) is None


# ── caps ──


def _holding(symbol, value, **kwargs):
    return Holding(symbol=symbol, market_value_base=value, **kwargs)


def test_caps_include_external_positions(seeded_store, engine):
    """NAV and every cap are defined over the whole book, not the part with an API."""
    with seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO external_positions (broker, symbol, quantity, currency, "
            "market_value_base, last_updated, sector, country) VALUES (?,?,?,?,?,?,?,?)",
            ("DEGIRO", "ASML", 10, "EUR", 30_000.0, "2026-03-01", "technology", "NL"),
        )

    report = engine.cap_utilisation(
        [_holding("SAN", 10_000.0, sector="financials", country="ES", currency="EUR")],
        nav=100_000.0,
        as_of=AS_OF,
    )

    symbols = {entry.key for entry in report.entries}
    assert "ASML" in symbols, "an external holding must count against the caps"
    technology = next(
        e for e in report.entries if e.dimension == CAP_SECTOR and e.key == "technology"
    )
    assert technology.pct_of_nav == pytest.approx(0.30)


def test_stale_external_data_makes_the_whole_report_unreliable(config, seeded_store, engine):
    stale_days = config.require_int("positions.external_data_stale_days")
    long_ago = (AS_OF - timedelta(days=stale_days + 5)).isoformat()
    with seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO external_positions (broker, symbol, quantity, currency, "
            "market_value_base, last_updated) VALUES (?,?,?,?,?,?)",
            ("REVOLUT", "VWCE", 5, "EUR", 5_000.0, long_ago),
        )

    report = engine.cap_utilisation([], nav=100_000.0, as_of=AS_OF)

    assert not report.reliable
    assert report.status == UNRELIABLE
    assert "REVOLUT:VWCE" in report.stale_holdings
    assert str(stale_days) in report.reason


def test_a_sector_cap_breach_is_flagged(config, engine):
    cap = config.require_float("positions.max_sector_pct_nav")
    report = engine.cap_utilisation(
        [
            _holding("A", 30_000.0, sector="technology", country="US", currency="USD"),
            _holding("B", 25_000.0, sector="technology", country="US", currency="USD"),
        ],
        nav=100_000.0,
        as_of=AS_OF,
        include_external=False,
    )
    sector = next(e for e in report.entries if e.dimension == CAP_SECTOR)
    assert sector.pct_of_nav > cap
    assert sector.breaches


def test_a_winner_past_its_initial_cap_is_reported_not_flagged(config, engine):
    """positions.rebalance_winners is false: a compounder is never trimmed to fit."""
    assert not engine.rebalance_winners
    cap = config.require_float("positions.max_initial_size_pct_nav")

    report = engine.cap_utilisation(
        [
            _holding(
                "WINNER",
                45_000.0,
                sector="technology",
                country="US",
                currency="USD",
                cost_basis_base=15_000.0,
                nav_at_purchase=100_000.0,
            )
        ],
        nav=100_000.0,
        as_of=AS_OF,
        include_external=False,
    )
    winner = next(e for e in report.entries if e.key == "WINNER")

    assert winner.pct_of_nav > cap
    assert not winner.breaches, "a winner past its cap is not a breach"
    assert winner.exceeds_by_compounding
    assert "not trimmed" in winner.note


def test_an_oversized_initial_purchase_is_a_breach(config, engine):
    report = engine.cap_utilisation(
        [
            _holding(
                "TOOBIG",
                40_000.0,
                cost_basis_base=40_000.0,
                nav_at_purchase=100_000.0,
                sector="energy",
                country="US",
                currency="USD",
            )
        ],
        nav=100_000.0,
        as_of=AS_OF,
        include_external=False,
    )
    entry = next(e for e in report.entries if e.key == "TOOBIG")
    assert entry.breaches
    assert not entry.exceeds_by_compounding


def test_an_orphan_position_is_called_out(config, engine):
    floor = config.require_float("positions.min_size_pct_nav")
    report = engine.cap_utilisation(
        [_holding("TINY", floor * 100_000.0 / 2, sector="energy", country="US", currency="USD")],
        nav=100_000.0,
        as_of=AS_OF,
        include_external=False,
    )
    tiny = next(e for e in report.entries if e.key == "TINY")
    assert "orphan" in tiny.note


def test_base_currency_exposure_is_not_a_foreign_currency_cap(config, engine):
    base = config.require_str("account.base_currency")
    report = engine.cap_utilisation(
        [
            _holding("EUROPEAN", 80_000.0, currency=base, sector="financials", country="ES"),
            _holding("US", 10_000.0, currency="USD", sector="technology", country="US"),
        ],
        nav=100_000.0,
        as_of=AS_OF,
        include_external=False,
    )
    currencies = {e.key for e in report.entries if e.dimension == "currency"}
    assert base not in currencies
    assert "USD" in currencies


def test_a_country_cap_is_reported_even_when_it_does_not_bind(config, engine):
    """A control that never binds documents intent; the user should know which."""
    report = engine.cap_utilisation(
        [_holding("A", 10_000.0, country="ES", sector="financials", currency="EUR")],
        nav=100_000.0,
        as_of=AS_OF,
        include_external=False,
    )
    country = next(e for e in report.entries if e.dimension == CAP_COUNTRY)
    assert not country.breaches
    assert country.cap == config.require_float("positions.max_country_pct_nav")


# ── circuit breaker ──


def test_the_breaker_reports_na_when_there_is_no_nav_history(engine):
    """No history is not "fine"; it is unmeasurable, and it says so."""
    breaker = engine.circuit_breaker(as_of=AS_OF)
    assert breaker.status == "n/a"
    assert not breaker.tripped
    assert "nav_history" in breaker.reason


def test_the_breaker_trips_past_the_configured_drawdown(config, engine):
    threshold = config.require_float("portfolio_circuit_breaker.monthly_drawdown_pct")
    engine.record_nav(as_of=AS_OF - timedelta(days=20), nav=100_000.0)
    engine.record_nav(as_of=AS_OF, nav=100_000.0 * (1 - threshold - 0.01))

    breaker = engine.circuit_breaker(as_of=AS_OF)

    assert breaker.status == "ok"
    assert breaker.tripped
    assert breaker.action == config.require_str("portfolio_circuit_breaker.action")


def test_a_shallow_drawdown_does_not_trip_the_breaker(config, engine):
    threshold = config.require_float("portfolio_circuit_breaker.monthly_drawdown_pct")
    engine.record_nav(as_of=AS_OF - timedelta(days=20), nav=100_000.0)
    engine.record_nav(as_of=AS_OF, nav=100_000.0 * (1 - threshold / 2))
    assert not engine.circuit_breaker(as_of=AS_OF).tripped


def test_a_drawdown_outside_the_window_is_not_counted(config, engine):
    window = config.require_int("portfolio_circuit_breaker.window_days")
    engine.record_nav(as_of=AS_OF - timedelta(days=window + 10), nav=200_000.0)
    engine.record_nav(as_of=AS_OF - timedelta(days=2), nav=100_000.0)
    engine.record_nav(as_of=AS_OF, nav=99_000.0)

    breaker = engine.circuit_breaker(as_of=AS_OF)
    assert breaker.peak_nav == 100_000.0
    assert not breaker.tripped


# ── averaging down ──


def test_averaging_down_is_refused_without_a_reunderwrite(seeded_store, engine, position_id):
    verdict = engine.averaging_down(get_position(seeded_store, position_id), as_of=AS_OF)
    assert not verdict.allowed
    assert "NEW, DATED catalyst" in verdict.reason
    assert "Falling price is not a catalyst" in verdict.reason


def test_a_logged_reunderwrite_with_a_future_catalyst_permits_it(seeded_store, engine, position_id):
    append_reunderwrite(
        seeded_store,
        position_id,
        dated_at="2026-02-01",
        catalyst="antitrust ruling scheduled",
        catalyst_date="2026-05-14",
    )
    verdict = engine.averaging_down(get_position(seeded_store, position_id), as_of=AS_OF)
    assert verdict.allowed
    assert verdict.catalyst_dates == ("2026-05-14",)


def test_a_catalyst_that_has_already_passed_does_not_permit_it(seeded_store, engine, position_id):
    """If the catalyst passed without the move, that is an invalidation."""
    append_reunderwrite(
        seeded_store,
        position_id,
        dated_at="2026-01-05",
        catalyst="earnings",
        catalyst_date="2026-01-20",
    )
    verdict = engine.averaging_down(get_position(seeded_store, position_id), as_of=AS_OF)
    assert not verdict.allowed


def test_a_dry_run_check_reports_the_streak_this_session_produces(
    seeded_store, config, engine, position_id
):
    """A check that reported yesterday's streak would say "not yet" on the session
    that actually confirms."""
    engine.zombie(dead_option(engine), position_id=position_id)

    later = engine.assess_option(
        as_of=AS_OF + timedelta(days=1),
        spot=90.0,
        strike=120.0,
        is_call=True,
        expiry=EXPIRY,
        premium_paid=3.00,
        current_bid=0.60,
        smile=dead_smile(),
        rate=RATE,
        dividend_yield=NO_DIVIDEND,
        prob_target_at_entry=0.30,
        exchange="ARCA",
    )
    dry = engine.zombie(later, position_id=position_id, record=False, book_penalty=False)

    assert dry.consecutive_sessions == 2
    assert dry.fires
    # And nothing was written or booked.
    stored = seeded_store.execute(
        "SELECT COUNT(*) FROM zombie_observations WHERE position_id = ?", (position_id,)
    ).fetchone()[0]
    assert stored == 1
    assert PenaltyLedger(seeded_store, config).balance() == 0.0


def test_re_reviewing_the_same_session_does_not_advance_the_streak(engine, position_id):
    """Two reviews on one day are one session, not two."""
    first = engine.zombie(dead_option(engine), position_id=position_id)
    again = engine.zombie(dead_option(engine), position_id=position_id)

    assert first.consecutive_sessions == 1
    assert again.consecutive_sessions == 1
    assert not again.fires


# ── the dividend yield ──
#
# `dividend_yield` was a parameter with a default of 0.0 that no caller ever
# overrode, so every payer in the universe was priced as a non-payer. The tests
# below are the reproduction from the Phase 0 audit, kept as the guard: they
# fail the moment anything reintroduces a silent zero.


def payer_smile():
    """A flat 28% surface around a spot of 60, spanning a strike of 66."""
    rows = [
        ChainRow(
            snapshot_date=AS_OF,
            underlying_id=1,
            expiry=EXPIRY,
            strike=strike,
            right="C",
            bid=0.43,
            ask=0.44,
            last=0.435,
            iv=0.28,
            delta=None,
            open_interest=2_000,
            volume=30,
            contract_id=None,
        )
        for strike in (54.0, 60.0, 66.0, 72.0, 78.0)
    ]
    return Smile.from_rows(rows, spot=60.0)


def payer_option(engine, *, dividend_yield):
    """The audit's case: S=60, K=66, 150 DTE, 28% IV."""
    return engine.assess_option(
        as_of=AS_OF,
        spot=60.0,
        strike=66.0,
        is_call=True,
        expiry=AS_OF + timedelta(days=150),
        premium_paid=2.00,
        current_bid=1.80,
        smile=payer_smile(),
        rate=RATE,
        dividend_yield=dividend_yield,
        stated_target=66.0,
        exchange="ARCA",
    )


def test_a_dividend_payers_probability_differs_from_the_zero_dividend_case(engine):
    """The reproduction from the Phase 0 audit, at the magnitudes it reported.

    q enters d1 as (r - q + sigma^2/2)*tau, so pricing a payer at zero returns an
    N(d2) that is too high. zombie_test.max_prob_ratio_vs_entry compares today's
    N(d2) against the one recorded at entry, which puts this bias directly on the
    exit gate: overstate the probability at entry and every later reading looks
    like a collapse; overstate it later and a dead position looks alive.
    """
    none_declared = payer_option(engine, dividend_yield=NO_DIVIDEND)
    payer = payer_option(engine, dividend_yield=0.05)

    assert payer.prob_target < none_declared.prob_target, (
        "N(d2) did not move with the dividend yield, so q is being discarded "
        "somewhere between assess_option and probability_itm"
    )
    overstatement = none_declared.prob_target / payer.prob_target - 1.0
    assert overstatement > 0.10, (
        f"the zero-dividend case overstates N(d2) by only {overstatement:.1%}; the audit "
        "measured about 15% on this case, so the yield is reaching the maths attenuated"
    )


def test_a_dividend_payers_delta_differs_from_the_zero_dividend_case(engine):
    """Delta carries exp(-q*tau) as an outright factor, not only through d1."""
    none_declared = payer_option(engine, dividend_yield=NO_DIVIDEND)
    payer = payer_option(engine, dividend_yield=0.05)

    gap = none_declared.delta - payer.delta
    assert gap > 0.03, (
        f"delta moved by only {gap:.4f} between q=0 and q=5%; the audit measured about "
        "0.048 on this case"
    )


def test_the_engine_refuses_to_assume_a_dividend_yield(engine):
    """No default. The defect was not the value zero, it was zero arriving silently.

    A caller that has not thought about q must be stopped by the signature rather
    than served a plausible number, which is what a default argument does.
    """
    with pytest.raises(TypeError):
        engine.assess_option(
            as_of=AS_OF,
            spot=60.0,
            strike=66.0,
            is_call=True,
            expiry=AS_OF + timedelta(days=150),
            premium_paid=2.00,
            current_bid=1.80,
            smile=payer_smile(),
            rate=RATE,
        )


def test_the_theta_budget_also_takes_the_dividend_yield(engine):
    """The same bias, on the entry gate rather than the exit one."""
    at_zero = engine.theta_budget(
        spot=60.0,
        strike=66.0,
        is_call=True,
        sigma=0.28,
        rate=RATE,
        premium_paid=2.00,
        entry_dte=150,
        expected_resolution_days=100,
        dividend_yield=NO_DIVIDEND,
    )
    for_a_payer = engine.theta_budget(
        spot=60.0,
        strike=66.0,
        is_call=True,
        sigma=0.28,
        rate=RATE,
        premium_paid=2.00,
        entry_dte=150,
        expected_resolution_days=100,
        dividend_yield=0.05,
    )
    assert for_a_payer.value_at_resolution < at_zero.value_at_resolution
