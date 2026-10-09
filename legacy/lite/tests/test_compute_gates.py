"""The gates, including the two the brief singles out.

A futures contract must never be returned as a tradeable candidate in ANY code
path, and an entry at 70 DTE must be rejected even though the minimum hold is 70
days. Both are tested here against the one implementation every caller uses,
which is the point of there being one.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from obliskiq.compute.gates import (
    GATE_CATALYST,
    GATE_INSTRUMENT,
    GATE_OPEN_INTEREST,
    GATE_POSITION_SIZE,
    GATE_PRICE_CEILING,
    GATE_PRIIPS,
    GATE_SPREAD,
    GATE_TENOR,
    GATE_VOLUME,
    CandidateGates,
)


@pytest.fixture()
def gates(config):
    return CandidateGates(config)


# ── tenor ──


def test_an_entry_at_seventy_dte_is_rejected(config, gates):
    """The headline case. hold_days_min is 70; entry_dte_min is deliberately 90.

    Entering at the minimum-hold figure lands the exit on expiry and violates the
    assignment rule, so the intended hold length is not a legal entry tenor.
    """
    hold_min = config.require_int("tenor.hold_days_min")
    entry_min = config.require_int("tenor.entry_dte_min")
    assert entry_min > hold_min, "config no longer separates entry tenor from hold length"

    result = gates.tenor(dte=hold_min)

    assert not result.passed
    assert GATE_TENOR in result.failed_gates
    assert str(hold_min) in result.reason()


def test_the_entry_window_boundaries_are_inclusive(config, gates):
    assert gates.tenor(dte=config.require_int("tenor.entry_dte_min")).passed
    assert gates.tenor(dte=config.require_int("tenor.entry_dte_max")).passed
    assert not gates.tenor(dte=config.require_int("tenor.entry_dte_max") + 1).passed


def test_an_expiry_inside_the_catalyst_buffer_is_rejected(config, gates):
    """Catalysts slip, and post-event repricing takes days."""
    buffer_days = config.require_int("tenor.catalyst_buffer_days")
    today = date(2026, 3, 2)
    catalyst = today + timedelta(days=100)

    too_close = gates.tenor(dte=100 + buffer_days - 1, catalyst_date=catalyst, today=today)
    clears = gates.tenor(dte=100 + buffer_days, catalyst_date=catalyst, today=today)

    assert GATE_CATALYST in too_close.failed_gates
    assert clears.passed


# ── prohibited instruments ──


def test_a_futures_contract_is_never_an_option_candidate(gates):
    result = gates.option_underlying({"symbol": "CL", "asset_class": "FUT"})
    assert not result.passed
    assert GATE_INSTRUMENT in result.failed_gates
    assert "prohibited" in result.reason()


def test_a_futures_contract_is_never_an_equity_candidate(gates):
    result = gates.equity_purchase({"symbol": "CL", "asset_class": "FUT", "tradable": 1})
    assert not result.passed
    assert GATE_INSTRUMENT in result.failed_gates


def test_no_gate_entry_point_ever_passes_a_futures_contract(gates):
    """Enumerated on purpose: the ban has to hold in every code path, not one."""
    future = {"symbol": "GC", "asset_class": "FUT", "tradable": 1, "priips_restricted": 0}
    for entry_point in (gates.option_underlying, gates.equity_purchase):
        assert not entry_point(future).passed, f"{entry_point.__name__} admitted a future"


def test_enabling_futures_options_still_bans_the_contract_itself(config):
    """allow_futures_options permits the OPTION. The contract stays prohibited."""
    import copy

    from obliskiq.config import Config

    data = copy.deepcopy(config.as_dict())
    data["instruments"]["allow_futures_options"] = True
    permissive = CandidateGates(Config(data, config.source))

    result = permissive.option_underlying({"symbol": "CL", "asset_class": "FUT"})
    assert not result.passed
    assert "never the contract itself" in result.reason()


def test_a_declared_leveraged_etp_is_refused_even_if_one_reached_the_gates(gates):
    """Belt and braces: ingestion drops these, and the gate would too."""
    result = gates.option_underlying({"symbol": "TQQQ", "asset_class": "ETF", "leveraged": True})
    assert not result.passed
    assert GATE_INSTRUMENT in result.failed_gates


# ── PRIIPs binds the cash instrument, not the option ──


def test_priips_blocks_the_cash_etf_but_not_its_options(gates):
    spy = {
        "symbol": "SPY",
        "asset_class": "ETF",
        "priips_restricted": 1,
        "tradable": 0,
        "untradeable_reason": "no PRIIPs KID for the cash ETF; options remain permitted",
    }
    assert GATE_PRIIPS in gates.equity_purchase(spy).failed_gates
    assert gates.option_underlying(spy).passed, (
        "the option sleeve exists precisely to trade options on PRIIPs-restricted ETFs"
    )


# ── liquidity ──


def test_thin_open_interest_fails_and_names_the_floor(config, gates):
    floor = config.require_int("liquidity.min_open_interest")
    result = gates.liquidity(spread_pct_of_mid=0.01, open_interest=floor - 1, volume=10)
    assert GATE_OPEN_INTEREST in result.failed_gates
    assert str(floor) in result.reason()


def test_a_wide_spread_fails_unless_the_book_is_deep(config, gates):
    ceiling = config.require_float("liquidity.max_spread_pct")
    waiver = config.require_int("liquidity.spread_waiver_min_open_interest")
    wide = ceiling * 2

    shallow = gates.liquidity(spread_pct_of_mid=wide, open_interest=waiver - 1, volume=10)
    deep = gates.liquidity(spread_pct_of_mid=wide, open_interest=waiver, volume=10)

    assert GATE_SPREAD in shallow.failed_gates
    assert deep.passed
    assert GATE_SPREAD in deep.waived, "the depth waiver is a rule, so the gate PASSED"
    assert GATE_SPREAD not in deep.untested
    assert deep.fully_tested, "a waived gate was applied; nothing went unmeasured"


def test_a_crossed_book_fails_outright_and_is_not_waivable_by_depth(config, gates):
    """A NEGATIVE spread (ask below bid) is a broken quote, not a tight one.

    Before this guard, `spread_pct_of_mid > ceiling` was the only comparison
    made: a negative fraction clears any positive ceiling, so a crossed book
    read as tighter than the tightest real quote on the board and passed
    outright. It must also survive the depth waiver, which exists for a WIDE
    spread on a deep book — a different claim from "this quote is not broken".
    """
    waiver = config.require_int("liquidity.spread_waiver_min_open_interest")

    shallow = gates.liquidity(spread_pct_of_mid=-0.90, open_interest=waiver - 1, volume=10)
    deep = gates.liquidity(spread_pct_of_mid=-0.90, open_interest=waiver + 1_000, volume=10)

    for result in (shallow, deep):
        assert not result.passed
        assert GATE_SPREAD in result.failed_gates
        assert GATE_SPREAD not in result.waived
        assert GATE_SPREAD not in result.untested


def test_an_unmeasurable_spread_is_untested_rather_than_waived(config, gates):
    """Two different things: a rule that waives a gate, and an input that is absent."""
    below_waiver = config.require_int("liquidity.spread_waiver_min_open_interest") - 1
    result = gates.liquidity(spread_pct_of_mid=None, open_interest=below_waiver, volume=10)
    assert GATE_SPREAD in result.untested
    assert GATE_SPREAD not in result.waived
    assert result.passed
    assert not result.fully_tested


def test_a_contract_that_did_not_trade_fails_the_volume_gate(config, gates):
    if not config.require_bool("liquidity.require_session_volume"):
        pytest.skip("session volume is not required by config")
    assert (
        GATE_VOLUME
        in gates.liquidity(spread_pct_of_mid=0.01, open_interest=10_000, volume=0).failed_gates
    )


def test_an_absent_input_leaves_a_gate_untested_rather_than_passed(gates):
    result = gates.liquidity(spread_pct_of_mid=None, open_interest=None, volume=None)
    assert result.passed, "nothing failed"
    assert set(result.untested) >= {GATE_OPEN_INTEREST, GATE_SPREAD}


# ── budget ──


def test_the_ceiling_is_the_sleeve_less_the_penalty_and_what_is_committed(config, gates):
    """Three subtractions, not two. The third is what makes budget_pct a cap."""
    budget_pct = config.require_float("option_sleeve.budget_pct")
    ceiling = gates.affordability(nav=200_000.0, penalty_balance=3_000.0, committed_premium=1_500.0)
    assert ceiling.available_budget == pytest.approx(budget_pct * 200_000.0 - 3_000.0 - 1_500.0)
    assert ceiling.committed_premium == pytest.approx(1_500.0)


def test_committed_premium_is_not_redistributed_across_the_remaining_slots(config, gates):
    """Unspent budget stays unspent.

    The tempting alternative — hand what is left to the positions still allowed
    — fills the sleeve by construction, which is the opposite of a cap.
    """
    budget_pct = config.require_float("option_sleeve.budget_pct")
    slots = config.require_int("option_sleeve.max_concurrent_positions")
    gross = budget_pct * 200_000.0

    ceiling = gates.affordability(nav=200_000.0, penalty_balance=0.0, committed_premium=1_000.0)

    assert ceiling.available_budget == pytest.approx(gross - 1_000.0)
    assert ceiling.available_budget != pytest.approx((gross - 1_000.0) / (slots - 1))


def test_a_forgotten_committed_premium_is_a_type_error_not_a_wrong_budget(gates):
    """The defect this argument closes must not be reachable by omission."""
    with pytest.raises(TypeError):
        gates.affordability(nav=200_000.0, penalty_balance=0.0)  # type: ignore[call-arg]


def test_the_budget_floors_at_zero_when_the_penalty_exceeds_the_sleeve(gates):
    ceiling = gates.affordability(nav=100_000.0, penalty_balance=1_000_000.0, committed_premium=0.0)
    assert ceiling.available_budget == 0.0
    assert ceiling.max_underlying_price(sigma=0.4, tau_years=0.5) == 0.0


def test_the_budget_floors_at_zero_when_commitments_exhaust_the_sleeve(gates):
    ceiling = gates.affordability(nav=100_000.0, penalty_balance=0.0, committed_premium=1_000_000.0)
    assert ceiling.available_budget == 0.0


def test_the_max_underlying_price_widens_as_volatility_falls(gates):
    """The affordability filter is volatility-scaled, like everything else here."""
    ceiling = gates.affordability(nav=200_000.0, penalty_balance=0.0, committed_premium=0.0)
    quiet = ceiling.max_underlying_price(sigma=0.20, tau_years=0.4)
    loud = ceiling.max_underlying_price(sigma=0.80, tau_years=0.4)
    assert quiet is not None and loud is not None
    assert quiet > loud


def test_the_price_ceiling_is_a_ceiling_and_not_a_floor(config, gates):
    """A cheap contract is not ineligible; it is bought in size.

    The premium floor is about the POSITION. Applying it per contract would
    report an entirely affordable universe as empty, which is a census saying
    "nothing qualifies" about a universe full of qualifying contracts.
    """
    multiplier = config.require_int("instruments.default_option_multiplier")
    ceiling = gates.affordability(nav=200_000.0, penalty_balance=0.0, committed_premium=0.0)

    cheap = ceiling.min_premium_position / multiplier / 10
    huge = ceiling.max_premium_per_contract / multiplier * 2

    base = ceiling.base_currency
    assert gates.price_ceiling(premium_per_share=cheap, ceiling=ceiling, currency=base).passed
    assert (
        GATE_PRICE_CEILING
        in gates.price_ceiling(premium_per_share=huge, ceiling=ceiling, currency=base).failed_gates
    )


def test_a_position_whose_costs_dominate_it_is_rejected(config, gates):
    """The floor refuses a position round-trip commission would eat, and only that."""
    multiplier = config.require_int("instruments.default_option_multiplier")
    ceiling = gates.affordability(nav=200_000.0, penalty_balance=0.0, committed_premium=0.0)
    per_share = ceiling.min_premium_position / multiplier / 10

    base = ceiling.base_currency
    assert (
        GATE_POSITION_SIZE
        in gates.position_size(
            premium_per_share=per_share, contracts=1, ceiling=ceiling, currency=base
        ).failed_gates
    )
    assert gates.position_size(
        premium_per_share=per_share, contracts=10, ceiling=ceiling, currency=base
    ).passed

    # And the same gate still closes at the other end: bought in enough size to
    # clear the floor, a cheap contract can still overrun the sleeve.
    too_many = gates.contracts_for_budget(premium_per_share=per_share, ceiling=ceiling) + 1
    assert (
        GATE_POSITION_SIZE
        in gates.position_size(
            premium_per_share=per_share, contracts=too_many, ceiling=ceiling, currency=base
        ).failed_gates
    )


def test_the_premium_floor_is_absolute_and_does_not_shrink_as_the_pool_drains(config, gates):
    """The regression the retired percentage floor had built into it.

    min_premium_pct_of_budget was measured against AVAILABLE budget, so every
    commitment lowered the floor and permitted a smaller next position — a floor
    that loosens exactly when it should bind. An absolute figure cannot.
    """
    floor = config.require_float("option_sleeve.min_premium_base_currency")
    empty = gates.affordability(nav=200_000.0, penalty_balance=0.0, committed_premium=0.0)
    nearly_spent = gates.affordability(
        nav=200_000.0, penalty_balance=0.0, committed_premium=empty.available_budget * 0.9
    )

    assert nearly_spent.available_budget < empty.available_budget
    assert empty.min_premium_position == pytest.approx(floor)
    assert nearly_spent.min_premium_position == pytest.approx(floor)


def test_the_budget_says_how_many_contracts_it_buys(config, gates):
    multiplier = config.require_int("instruments.default_option_multiplier")
    ceiling = gates.affordability(nav=200_000.0, penalty_balance=0.0, committed_premium=0.0)
    per_share = ceiling.max_premium_per_contract / multiplier / 4

    assert gates.contracts_for_budget(premium_per_share=per_share, ceiling=ceiling) == 4
    assert gates.contracts_for_budget(premium_per_share=0.0, ceiling=ceiling) == 0


# ── implied volatility ──


def test_the_iv_percentile_gate_is_the_bottom_quartile(config, gates):
    ceiling = config.require_float("screen.cheap_iv_percentile_max")
    assert gates.iv_percentile(ceiling).passed
    assert not gates.iv_percentile(ceiling + 0.01).passed
    assert gates.iv_percentile(None).untested


# ── the currency gap Session 6 found, now closed ──

#: EUR/USD on 2026-08-24, the day the IWM case below was measured.
EURUSD_ON_THE_DAY = 1.1683
#: The multiplier taking a USD amount to EUR. The config series is USD per EUR,
#: so BaseCurrencyRates inverts it once; this mirrors that arithmetic.
USD_TO_EUR = 1.0 / EURUSD_ON_THE_DAY


def test_the_ceiling_names_the_currency_it_is_denominated_in(config, gates):
    """A number without its unit is not data, and every gate message states it."""
    base = config.require_str("account.base_currency")
    ceiling = gates.affordability(nav=11_168.40, penalty_balance=0.0, committed_premium=0.0)
    assert ceiling.base_currency == base


def test_a_foreign_premium_inside_the_sleeve_is_admitted_once_converted(gates):
    """A contract that fits in base currency and does not fit in its own.

    Measured live before the conversion existed: a premium a shade over the
    sleeve in the CONTRACT's currency, and comfortably inside it once converted.
    The error runs in both directions and its size is the exchange rate.

    Without a rate the gate reports UNTESTED — never a raw comparison of two
    different currencies, which is the assumption in the permissive direction
    that this whole thread exists to close.
    """
    ceiling = gates.affordability(nav=10_000.0, penalty_balance=0.0, committed_premium=0.0)
    # One contract just over the ceiling in its own currency.
    per_share = (ceiling.max_premium_per_contract / ceiling.contract_multiplier) * 1.05

    no_rate = gates.price_ceiling(premium_per_share=per_share, ceiling=ceiling, currency="USD")
    assert GATE_PRICE_CEILING in no_rate.untested, "a missing rate must not be compared raw"
    assert not no_rate.fully_tested
    assert GATE_PRICE_CEILING not in no_rate.failed_gates, (
        "untested is not failed either: the caller is told the gate could not run"
    )

    converted = gates.price_ceiling(
        premium_per_share=per_share, ceiling=ceiling, currency="USD", fx_to_base=USD_TO_EUR
    )
    assert converted.passed, "converted, the same premium fits"


def test_a_premium_in_an_undeclared_currency_is_never_compared(gates):
    """universe.currency is NOT NULL, so a row without one is a gap, not a base amount.

    Reading a missing currency as "already in base" compared a foreign premium
    against a base-currency budget and said nothing about having done so.
    """
    ceiling = gates.affordability(nav=10_000.0, penalty_balance=0.0, committed_premium=0.0)
    verdict = gates.price_ceiling(premium_per_share=1.0, ceiling=ceiling, currency=None)
    assert GATE_PRICE_CEILING in verdict.untested
    assert not verdict.fully_tested, "an untested gate is never a passed gate"


def test_a_contract_the_sleeve_truly_cannot_afford_is_still_refused(gates):
    """Conversion must not become a way in. USD 800 is EUR 685 and still too much."""
    ceiling = gates.affordability(nav=11_168.40, penalty_balance=0.0, committed_premium=0.0)
    verdict = gates.price_ceiling(
        premium_per_share=8.00, ceiling=ceiling, currency="USD", fx_to_base=USD_TO_EUR
    )
    assert not verdict.passed
    assert verdict.failures[0].observed == pytest.approx(800 * USD_TO_EUR)


def test_a_base_currency_premium_needs_no_rate(gates):
    """The single-currency path must not start demanding an exchange rate."""
    ceiling = gates.affordability(nav=11_168.40, penalty_balance=0.0, committed_premium=0.0)
    verdict = gates.price_ceiling(premium_per_share=4.00, ceiling=ceiling, currency="EUR")
    assert verdict.passed


def test_a_missing_rate_leaves_the_gate_untested_rather_than_assuming_parity(gates):
    """Assuming 1.0 would ADMIT contracts the sleeve cannot afford.

    That is the one direction this gate must never fail in, so an absent rate
    reports UNTESTED instead of guessing. Untested is not the same as passed:
    ``GateResult.passed`` only means nothing failed, and the screener reports a
    candidate carrying an untested gate as ``incomplete`` rather than as a
    survivor — see test_compute_screen.py.
    """
    ceiling = gates.affordability(nav=11_168.40, penalty_balance=0.0, committed_premium=0.0)
    verdict = gates.price_ceiling(
        premium_per_share=5.60, ceiling=ceiling, currency="USD", fx_to_base=None
    )
    assert GATE_PRICE_CEILING in verdict.untested
    assert not verdict.failures, "an unknown rate must not be reported as a refusal either"


def test_position_size_converts_on_both_the_ceiling_and_the_floor(gates):
    """min_premium_base_currency names its unit; comparing a USD cost to it was
    the same defect price_ceiling carried."""
    ceiling = gates.affordability(nav=11_168.40, penalty_balance=0.0, committed_premium=0.0)

    # USD 25 is EUR 21.40 — above the EUR 20 floor once converted, below it if
    # the conversion is skipped in the other direction.
    verdict = gates.position_size(
        premium_per_share=0.25, contracts=1, ceiling=ceiling, currency="USD", fx_to_base=USD_TO_EUR
    )
    assert verdict.passed

    missing_rate = gates.position_size(
        premium_per_share=0.25, contracts=1, ceiling=ceiling, currency="USD", fx_to_base=None
    )
    assert GATE_POSITION_SIZE in missing_rate.untested
