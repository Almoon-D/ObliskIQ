"""The candidate gates (lite's ``tests/test_compute_gates.py``, ported), including the two lite
singles out: a futures contract is never a candidate in any code path, and an entry at the
minimum-hold tenor is refused although the intended hold is that long. Each refusal carries its
reason code; an absent input leaves its gate untested, never passed.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_universe import UniverseInstrument
from obliskiq.decision.gates import (
    Affordability,
    CandidateGates,
    Gate,
    GateResult,
    OptionCandidate,
    OptionEntry,
    merged,
)
from obliskiq.domain.enums import AssetClass, OptionRight
from obliskiq.domain.market import ChainRow
from obliskiq.quant.smile import Smile, SmileFilters

AS_OF = date(2026, 3, 2)
#: EUR/USD on the day lite measured the currency gap: the USD -> EUR multiplier.
USD_TO_EUR = Decimal(1) / Decimal("1.1683")


@pytest.fixture
def gates(shipped_settings: ObliskConfig) -> CandidateGates:
    return CandidateGates(shipped_settings)


def spy(settings: ObliskConfig, **attributes: Any) -> UniverseInstrument:
    declared = settings.universe.instrument("SPY")
    assert declared is not None
    return declared.model_copy(update=attributes)


def ceiling(
    gates: CandidateGates, budget: str = "10000", penalty: str = "0", committed: str = "0"
) -> Affordability:
    return gates.affordability(
        budget=Decimal(budget),
        penalty_balance=Decimal(penalty),
        committed_premium=Decimal(committed),
    )


# ── tenor ──


def test_an_entry_at_the_minimum_hold_is_refused(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    """The headline case: hold_days_min is 70, entry_dte_min deliberately 90."""
    tenor = shipped_settings.tenor
    assert tenor.entry_dte_min > tenor.hold_days_min
    result = gates.tenor(dte=tenor.hold_days_min, catalyst_date=None, as_of=AS_OF)
    assert result.failed_gates == (Gate.TENOR,)
    assert str(tenor.hold_days_min) in (result.reason() or "")


def test_the_entry_window_is_inclusive(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    tenor = shipped_settings.tenor
    assert gates.tenor(dte=tenor.entry_dte_min, catalyst_date=None, as_of=AS_OF).passed
    assert gates.tenor(dte=tenor.entry_dte_max, catalyst_date=None, as_of=AS_OF).passed
    beyond = gates.tenor(dte=tenor.entry_dte_max + 1, catalyst_date=None, as_of=AS_OF)
    assert beyond.failed_gates == (Gate.TENOR,)


def test_an_expiry_inside_the_catalyst_buffer_is_refused(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    buffer = shipped_settings.tenor.catalyst_buffer_days
    catalyst = AS_OF + timedelta(days=100)
    close = gates.tenor(dte=100 + buffer - 1, catalyst_date=catalyst, as_of=AS_OF)
    clears = gates.tenor(dte=100 + buffer, catalyst_date=catalyst, as_of=AS_OF)
    assert close.failed_gates == (Gate.CATALYST,)
    assert clears.passed


def test_the_mandatory_close_refuses_an_entry_inside_its_window(gates: CandidateGates) -> None:
    assert gates.mandatory_close(close_by=AS_OF + timedelta(days=1), as_of=AS_OF).passed
    assert gates.mandatory_close(close_by=AS_OF, as_of=AS_OF).failed_gates == (
        Gate.MANDATORY_CLOSE,
    )
    unknown = gates.mandatory_close(close_by=None, as_of=AS_OF, gap="the calendars cannot")
    assert unknown.untested_gates == (Gate.MANDATORY_CLOSE,)
    assert unknown.untested[0].reason == "the calendars cannot"


# ── prohibited instruments ──


def test_a_futures_underlying_is_never_a_candidate(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    future = spy(shipped_settings, asset_class=AssetClass.FUTURE, symbol="CL")
    result = gates.option_underlying(future)
    assert result.failed_gates == (Gate.INSTRUMENT,)
    assert "futures" in (result.reason() or "")


def test_a_leveraged_description_is_refused_and_an_unknown_one_too(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    leveraged = spy(shipped_settings, description="PROSHARES ULTRAPRO QQQ")
    assert gates.option_underlying(leveraged).failed_gates == (Gate.INSTRUMENT,)
    unknown = spy(shipped_settings, description=None)
    result = gates.option_underlying(unknown)
    assert result.failed_gates == (Gate.INSTRUMENT,)
    assert "no resolved description" in (result.reason() or "")


def test_priips_is_not_consulted_for_an_option_underlying(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    """The cash ETF is not purchasable (no KID); the option sleeve exists to trade its options."""
    assert gates.option_underlying(spy(shipped_settings)).passed


# ── liquidity ──


def test_thin_open_interest_fails_and_names_the_floor(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    floor = shipped_settings.liquidity.min_open_interest
    result = gates.liquidity(spread_pct_of_mid=0.01, open_interest=floor - 1, volume=10)
    assert result.failed_gates == (Gate.OPEN_INTEREST,)
    assert str(floor) in (result.reason() or "")


def test_a_wide_spread_fails_unless_the_book_is_deep(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    liquidity = shipped_settings.liquidity
    wide = liquidity.max_spread_pct * 2
    waiver = liquidity.spread_waiver_min_open_interest
    shallow = gates.liquidity(spread_pct_of_mid=wide, open_interest=waiver - 1, volume=10)
    deep = gates.liquidity(spread_pct_of_mid=wide, open_interest=waiver, volume=10)
    assert shallow.failed_gates == (Gate.SPREAD,)
    assert deep.passed
    assert deep.fully_tested
    assert deep.waived == (Gate.SPREAD,)


def test_a_crossed_book_fails_and_no_depth_waives_it(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    waiver = shipped_settings.liquidity.spread_waiver_min_open_interest
    for depth in (waiver - 1, waiver + 1000):
        result = gates.liquidity(spread_pct_of_mid=-0.9, open_interest=depth, volume=10)
        assert result.failed_gates == (Gate.SPREAD,)
        assert Gate.SPREAD not in result.waived
        assert "crossed" in (result.reason() or "")


def test_an_unmeasurable_spread_is_untested_not_waived(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    below = shipped_settings.liquidity.spread_waiver_min_open_interest - 1
    result = gates.liquidity(spread_pct_of_mid=None, open_interest=below, volume=10)
    assert result.untested_gates == (Gate.SPREAD,)
    assert result.passed
    assert not result.fully_tested
    assert Gate.SPREAD not in result.waived


def test_a_contract_that_did_not_trade_fails_volume(gates: CandidateGates) -> None:
    result = gates.liquidity(spread_pct_of_mid=0.01, open_interest=10_000, volume=0)
    assert result.failed_gates == (Gate.VOLUME,)


def test_absent_inputs_leave_their_gates_untested(gates: CandidateGates) -> None:
    result = gates.liquidity(spread_pct_of_mid=None, open_interest=None, volume=None)
    assert result.passed
    assert set(result.untested_gates) == {Gate.OPEN_INTEREST, Gate.SPREAD, Gate.VOLUME}


def test_volume_is_not_tested_when_configuration_does_not_require_it(
    shipped_settings: ObliskConfig,
) -> None:
    liquidity = shipped_settings.liquidity.model_copy(update={"require_session_volume": False})
    lax = CandidateGates(shipped_settings.model_copy(update={"liquidity": liquidity}))
    assert lax.liquidity(spread_pct_of_mid=0.01, open_interest=1000, volume=None).fully_tested


# ── the smile ──


def smile_of(make_chain_row: Callable[..., ChainRow], settings: ObliskConfig) -> Smile:
    rows = [
        make_chain_row(strike=strike, delta=delta, right=right)
        for strike, delta, right in (
            (95.0, -0.35, OptionRight.PUT),
            (100.0, 0.5, OptionRight.CALL),
            (105.0, 0.35, OptionRight.CALL),
        )
    ]
    found = Smile.from_rows(rows, spot=100.0, filters=SmileFilters.from_config(settings))
    assert found is not None
    return found


def test_a_strike_on_the_smile_passes_and_one_off_it_fails(
    shipped_settings: ObliskConfig,
    gates: CandidateGates,
    make_chain_row: Callable[..., ChainRow],
) -> None:
    smile = smile_of(make_chain_row, shipped_settings)
    assert gates.smile(smile, strike=100.0).passed
    clamped = gates.smile(smile, strike=120.0)
    assert clamped.failed_gates == (Gate.SMILE,)
    assert "clamped" in (clamped.reason() or "")


def test_no_smile_fails_because_the_zombie_test_could_never_run(gates: CandidateGates) -> None:
    result = gates.smile(None, strike=100.0)
    assert result.failed_gates == (Gate.SMILE,)
    assert "zombie" in (result.reason() or "")


# ── the pool ──


def test_the_pool_is_the_budget_less_the_penalty_and_the_commitments(
    gates: CandidateGates,
) -> None:
    pool = ceiling(gates, "10000", "3000", "1500")
    assert pool.available == Decimal("5500")
    assert pool.locked_by is None


def test_unspent_budget_stays_unspent(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    """Not redistributed across the remaining slots: that rule fills the sleeve by construction."""
    slots = shipped_settings.option_sleeve.max_concurrent_positions
    pool = ceiling(gates, "10000", "0", "1000")
    assert pool.available == Decimal("9000")
    assert pool.available != Decimal("9000") / (slots - 1)


def test_a_forgotten_commitment_is_a_type_error_not_a_wrong_budget(gates: CandidateGates) -> None:
    with pytest.raises(TypeError):
        gates.affordability(budget=Decimal(1), penalty_balance=Decimal(0))  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("penalty", "committed", "locked_by"),
    [
        ("100000", "0", "penalty"),
        ("0", "100000", "commitments"),
        ("100000", "100000", "penalty_and_commitments"),
    ],
)
def test_a_locked_pool_says_what_locked_it(
    gates: CandidateGates, penalty: str, committed: str, locked_by: str
) -> None:
    pool = ceiling(gates, "10000", penalty, committed)
    assert pool.available == 0
    assert pool.locked_by == locked_by


def test_a_pool_with_no_budget_names_that(gates: CandidateGates) -> None:
    assert ceiling(gates, "0").locked_by == "no_budget"


def test_the_concurrency_cap_is_the_only_anti_fragmentation_rule(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    cap = shipped_settings.option_sleeve.max_concurrent_positions
    assert gates.concurrency(open_positions=cap - 1).passed
    assert gates.concurrency(open_positions=cap).failed_gates == (Gate.CONCURRENCY,)
    assert gates.concurrency(open_positions=None).untested_gates == (Gate.CONCURRENCY,)


def test_the_price_ceiling_is_a_ceiling_and_not_a_floor(gates: CandidateGates) -> None:
    pool = ceiling(gates, "1000")
    cheap = gates.price_ceiling(
        premium_per_unit=0.01, multiplier=100.0, currency="EUR", fx_to_base=None, ceiling=pool
    )
    dear = gates.price_ceiling(
        premium_per_unit=20.0, multiplier=100.0, currency="EUR", fx_to_base=None, ceiling=pool
    )
    assert cheap.passed
    assert dear.failed_gates == (Gate.PRICE_CEILING,)


def test_a_locked_pool_refuses_every_contract_and_says_why(gates: CandidateGates) -> None:
    pool = ceiling(gates, "1000", penalty="5000")
    result = gates.price_ceiling(
        premium_per_unit=0.5, multiplier=100.0, currency="EUR", fx_to_base=None, ceiling=pool
    )
    assert result.failed_gates == (Gate.PRICE_CEILING,)
    assert "locked by penalty" in (result.reason() or "")


def test_a_position_whose_costs_dominate_it_is_refused(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    floor = shipped_settings.option_sleeve.min_premium_base_currency
    pool = ceiling(gates, "10000")
    per_unit = float(floor) / 100 / 10
    one = gates.position_size(
        premium_per_unit=per_unit,
        multiplier=100.0,
        contracts=1,
        currency="EUR",
        fx_to_base=None,
        ceiling=pool,
    )
    ten = gates.position_size(
        premium_per_unit=per_unit,
        multiplier=100.0,
        contracts=10,
        currency="EUR",
        fx_to_base=None,
        ceiling=pool,
    )
    assert one.failed_gates == (Gate.POSITION_SIZE,)
    assert "floor" in (one.reason() or "")
    assert ten.passed
    unit = Decimal(repr(per_unit)) * 100
    too_many = gates.contracts_for_budget(unit_cost_base=unit, ceiling=pool) + 1
    over = gates.position_size(
        premium_per_unit=per_unit,
        multiplier=100.0,
        contracts=too_many,
        currency="EUR",
        fx_to_base=None,
        ceiling=pool,
    )
    assert over.failed_gates == (Gate.POSITION_SIZE,)


def test_the_floor_is_absolute_and_does_not_shrink_as_the_pool_drains(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    floor = shipped_settings.option_sleeve.min_premium_base_currency
    empty = ceiling(gates, "10000")
    drained = ceiling(gates, "10000", committed="9000")
    assert drained.available < empty.available
    assert empty.min_premium_position == drained.min_premium_position == floor


def test_a_contract_count_must_be_positive(gates: CandidateGates) -> None:
    result = gates.position_size(
        premium_per_unit=1.0,
        multiplier=100.0,
        contracts=0,
        currency="EUR",
        fx_to_base=None,
        ceiling=ceiling(gates),
    )
    assert result.failed_gates == (Gate.POSITION_SIZE,)


def test_the_budget_says_how_many_contracts_it_buys(gates: CandidateGates) -> None:
    pool = ceiling(gates, "1000")
    assert gates.contracts_for_budget(unit_cost_base=Decimal(250), ceiling=pool) == 4
    assert gates.contracts_for_budget(unit_cost_base=Decimal(0), ceiling=pool) == 0


# ── currencies ──


def test_a_foreign_premium_is_compared_converted_or_not_at_all(gates: CandidateGates) -> None:
    pool = ceiling(gates, "1000")
    per_unit = float(pool.available) / 100 * 1.05  # just over the pool in its own currency
    no_rate = gates.price_ceiling(
        premium_per_unit=per_unit, multiplier=100.0, currency="USD", fx_to_base=None, ceiling=pool
    )
    assert no_rate.untested_gates == (Gate.PRICE_CEILING,)
    assert not no_rate.failures
    converted = gates.price_ceiling(
        premium_per_unit=per_unit,
        multiplier=100.0,
        currency="USD",
        fx_to_base=USD_TO_EUR,
        ceiling=pool,
    )
    assert converted.passed


def test_conversion_is_never_a_way_in(gates: CandidateGates) -> None:
    """USD 800 is about EUR 685: still beyond a EUR 500 pool."""
    result = gates.price_ceiling(
        premium_per_unit=8.0,
        multiplier=100.0,
        currency="USD",
        fx_to_base=USD_TO_EUR,
        ceiling=ceiling(gates, "500"),
    )
    assert result.failed_gates == (Gate.PRICE_CEILING,)
    assert result.failures[0].observed == pytest.approx(800 / 1.1683)


def test_a_missing_quote_multiplier_or_budget_is_untested(gates: CandidateGates) -> None:
    pool = ceiling(gates)
    for kwargs in (
        {"premium_per_unit": None, "multiplier": 100.0, "ceiling": pool},
        {"premium_per_unit": 1.0, "multiplier": None, "ceiling": pool},
        {"premium_per_unit": 1.0, "multiplier": 100.0, "ceiling": None},
    ):
        result = gates.price_ceiling(currency="EUR", fx_to_base=None, **kwargs)  # type: ignore[arg-type]
        assert result.untested_gates == (Gate.PRICE_CEILING,), kwargs


def test_position_size_converts_too(gates: CandidateGates) -> None:
    """USD 25 is EUR 21.40, above the EUR 20 floor once converted."""
    pool = ceiling(gates)
    passes = gates.position_size(
        premium_per_unit=0.25,
        multiplier=100.0,
        contracts=1,
        currency="USD",
        fx_to_base=USD_TO_EUR,
        ceiling=pool,
    )
    missing = gates.position_size(
        premium_per_unit=0.25,
        multiplier=100.0,
        contracts=1,
        currency="USD",
        fx_to_base=None,
        ceiling=pool,
    )
    assert passes.passed
    assert missing.untested_gates == (Gate.POSITION_SIZE,)


# ── the screen and entry discipline ──


def test_the_iv_percentile_gate_is_the_cheap_quartile(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    cheap = shipped_settings.screen.cheap_iv_percentile_max
    assert gates.iv_percentile(cheap).passed
    assert gates.iv_percentile(cheap + 0.01).failed_gates == (Gate.IV_PERCENTILE,)
    assert gates.iv_percentile(None).untested_gates == (Gate.IV_PERCENTILE,)


def test_the_target_must_pay_the_payoff_ratio(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    ratio = shipped_settings.option_sleeve.min_payoff_ratio
    good = gates.payoff_ratio(target=100 + ratio * 2, strike=100, premium_per_unit=2, is_call=True)
    short = gates.payoff_ratio(target=101, strike=100, premium_per_unit=2, is_call=True)
    put = gates.payoff_ratio(target=100 - ratio * 2, strike=100, premium_per_unit=2, is_call=False)
    assert good.passed
    assert put.passed
    assert short.failed_gates == (Gate.PAYOFF_RATIO,)
    unnamed = gates.payoff_ratio(target=None, strike=100, premium_per_unit=2, is_call=True)
    assert unnamed.failed_gates == (Gate.PAYOFF_RATIO,), "only the caller can state a target"
    unpriced = gates.payoff_ratio(target=110, strike=100, premium_per_unit=None, is_call=True)
    assert unpriced.untested_gates == (Gate.PAYOFF_RATIO,)


def test_a_top_decile_entry_needs_a_catalyst(
    shipped_settings: ObliskConfig, gates: CandidateGates
) -> None:
    top = shipped_settings.discipline.entry_top_decile_pctile
    refused = gates.entry_range(entry_range_pctile=top, catalyst_date=None)
    assert refused.failed_gates == (Gate.ENTRY_RANGE,)
    assert gates.entry_range(entry_range_pctile=top, catalyst_date=AS_OF).passed
    assert gates.entry_range(entry_range_pctile=top - 0.01, catalyst_date=None).passed
    assert gates.entry_range(entry_range_pctile=None, catalyst_date=None).untested_gates == (
        Gate.ENTRY_RANGE,
    )


def test_the_entry_range_rule_can_be_turned_off(shipped_settings: ObliskConfig) -> None:
    discipline = shipped_settings.discipline.model_copy(
        update={"entry_top_decile_requires_catalyst": False}
    )
    lax = CandidateGates(shipped_settings.model_copy(update={"discipline": discipline}))
    assert lax.entry_range(entry_range_pctile=0.99, catalyst_date=None).fully_tested


def test_the_theta_budget_gate(shipped_settings: ObliskConfig, gates: CandidateGates) -> None:
    floor = shipped_settings.zombie_test.min_recoverable_pct_of_premium
    assert gates.theta_budget(floor).passed
    assert gates.theta_budget(floor - 0.01).failed_gates == (Gate.THETA_BUDGET,)
    assert gates.theta_budget(None).untested_gates == (Gate.THETA_BUDGET,)
    tenor = shipped_settings.tenor.model_copy(update={"require_theta_budget_check": False})
    lax = CandidateGates(shipped_settings.model_copy(update={"tenor": tenor}))
    assert lax.theta_budget(None).fully_tested


# ── the composition ──


def candidate(
    settings: ObliskConfig, row: ChainRow, *, smile: Smile | None, **kw: Any
) -> OptionCandidate:
    fields: dict[str, Any] = {
        "row": row,
        "underlying": spy(settings),
        "as_of": AS_OF,
        "currency": "USD",
        "smile": smile,
        "iv_percentile": 0.1,
        "close_by": AS_OF + timedelta(days=100),
        "affordability": CandidateGates(settings).affordability(
            budget=Decimal(5000), penalty_balance=Decimal(0), committed_premium=Decimal(0)
        ),
        "fx_to_base": USD_TO_EUR,
        "open_positions": 0,
    }
    fields.update(kw)
    return OptionCandidate(**fields)


def test_a_clean_candidate_clears_every_gate_with_every_gate_applied(
    shipped_settings: ObliskConfig,
    gates: CandidateGates,
    make_chain_row: Callable[..., ChainRow],
) -> None:
    smile = smile_of(make_chain_row, shipped_settings)
    row = make_chain_row(expiry=AS_OF + timedelta(days=120), multiplier=100.0)
    result = gates.candidate(candidate(shipped_settings, row, smile=smile))
    assert result.fully_tested, result


def test_each_input_absent_leaves_its_gate_untested_with_why(
    shipped_settings: ObliskConfig,
    gates: CandidateGates,
    make_chain_row: Callable[..., ChainRow],
) -> None:
    smile = smile_of(make_chain_row, shipped_settings)
    row = make_chain_row(expiry=AS_OF + timedelta(days=120), multiplier=100.0)
    found = gates.candidate(
        candidate(
            shipped_settings,
            row,
            smile=smile,
            iv_percentile=None,
            iv_percentile_gap="no surface",
            close_by=None,
            close_by_gap="no calendar",
            affordability=None,
            affordability_gap="no budget",
            open_positions=None,
            open_positions_gap="no count",
        )
    )
    assert found.passed
    assert not found.fully_tested
    reasons = {note.gate: note.reason for note in found.untested}
    assert reasons == {
        Gate.IV_PERCENTILE: "no surface",
        Gate.MANDATORY_CLOSE: "no calendar",
        Gate.PRICE_CEILING: "no budget",
        Gate.CONCURRENCY: "no count",
    }


def test_the_entry_adds_size_payoff_range_and_theta(
    shipped_settings: ObliskConfig,
    gates: CandidateGates,
    make_chain_row: Callable[..., ChainRow],
) -> None:
    smile = smile_of(make_chain_row, shipped_settings)
    row = make_chain_row(expiry=AS_OF + timedelta(days=120), multiplier=100.0)
    terms = OptionEntry(
        contracts=1,
        target=100.0 + 5.1 * 4,
        entry_range_pctile=0.5,
        theta_remaining_share=0.6,
    )
    assert gates.entry(candidate(shipped_settings, row, smile=smile), terms).fully_tested
    weak = OptionEntry(
        contracts=1, target=101.0, entry_range_pctile=0.95, theta_remaining_share=0.01
    )
    result = gates.entry(candidate(shipped_settings, row, smile=smile), weak)
    assert set(result.failed_gates) == {Gate.PAYOFF_RATIO, Gate.ENTRY_RANGE, Gate.THETA_BUDGET}


def test_results_merge_and_report(gates: CandidateGates) -> None:
    combined = merged(
        [
            gates.iv_percentile(0.9),
            gates.liquidity(spread_pct_of_mid=None, open_interest=None, volume=None),
            GateResult(),
        ]
    )
    assert combined.failed_gates == (Gate.IV_PERCENTILE,)
    assert "IV percentile" in (combined.reason() or "")
    assert GateResult().reason() is None
