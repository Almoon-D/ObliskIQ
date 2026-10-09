"""The gates that close before a ledger write, in the server rather than upstream.

``store.ledger.open_position`` enforces ledger integrity and nothing about the
mandate — it can see neither the chain nor the sleeve. These are the checks that
make ``log_entry`` server-side enforcement rather than a pass-through, and the
reason the skill is never the only thing between a bad trade and the ledger.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from obliskiq.compute.gates import (
    GATE_ENTRY_RANGE,
    GATE_INSTRUMENT,
    GATE_PAYOFF_RATIO,
    GATE_POSITION_SIZE,
    GATE_PRICE_CEILING,
    GATE_TENOR,
    GATE_THETA_BUDGET,
)
from obliskiq.compute.penalty import PenaltyLedger
from obliskiq.server.guards import (
    GATE_CONCURRENCY,
    GATE_CONTRACT_COUNT,
    GATE_MANDATORY_CLOSE,
    EntryGuard,
)
from obliskiq.store.ledger import open_position

SESSION = date(2026, 1, 15)
NAV = 200_000.0

THESIS = "IV in the bottom decile into a dated regulatory ruling"
INVALIDATION = "spot closes below 88.00 on any session"

#: A second instrument, so a pool test is not also an averaging-down test.
_OTHER_INSTRUMENT: dict[str, object] = {
    "contract_id": 2,
    "symbol": "OTHER",
    "asset_class": "STK",
    "currency": "EUR",
    "tradable": 1,
    "priips_restricted": 0,
}


@pytest.fixture()
def guard(seeded_store, config):
    return EntryGuard(seeded_store, config)


@pytest.fixture()
def equity() -> dict[str, object]:
    return {
        "contract_id": 1,
        "symbol": "TEST",
        "asset_class": "STK",
        "currency": "EUR",
        "tradable": 1,
        "priips_restricted": 0,
    }


def _close_date(guard: EntryGuard, expiry: date) -> date:
    """The close date the calendar computes, which the guard requires a match to."""
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument={"contract_id": 1, "symbol": "TEST", "asset_class": "STK"},
        is_option=True,
        nav=NAV,
        expiry=expiry,
    )
    assert verdict.computed_close is not None
    return verdict.computed_close.close_date


def test_an_entry_at_seventy_dte_is_rejected(guard, equity):
    """tenor.entry_dte_min is deliberately above tenor.hold_days_min.

    Entering at the minimum-hold figure lands the exit on expiry and violates the
    assignment rule, which is why 70 DTE against a 70-day minimum hold fails.
    """
    expiry = SESSION + timedelta(days=70)
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument=equity,
        is_option=True,
        nav=NAV,
        expiry=expiry,
        premium_per_share=2.5,
        mandatory_close_date=_close_date(guard, expiry),
    )
    assert not verdict.allowed
    assert GATE_TENOR in verdict.gates.failed_gates
    assert "70" in (verdict.reason or "")


def test_an_entry_inside_the_window_clears_the_tenor_gate(guard, equity):
    expiry = SESSION + timedelta(days=150)
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument=equity,
        is_option=True,
        nav=NAV,
        expiry=expiry,
        premium_per_share=2.5,
        contracts=1,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
        mandatory_close_date=_close_date(guard, expiry),
    )
    assert verdict.allowed, verdict.reason


def test_an_option_entry_without_a_contract_count_is_refused(guard, equity):
    """The sleeve is a pool, so an unsized commitment cannot be subtracted from it."""
    expiry = SESSION + timedelta(days=150)
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument=equity,
        is_option=True,
        nav=NAV,
        expiry=expiry,
        premium_per_share=2.5,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
        mandatory_close_date=_close_date(guard, expiry),
    )
    assert not verdict.allowed
    assert GATE_CONTRACT_COUNT in verdict.gates.failed_gates


def test_a_futures_contract_is_never_a_candidate(guard):
    """Prohibited in every code path, including the one that writes the ledger."""
    expiry = SESSION + timedelta(days=150)
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument={"contract_id": 1, "symbol": "CL", "asset_class": "FUT"},
        is_option=False,
        nav=NAV,
        expiry=expiry,
        premium_per_share=2.5,
    )
    assert not verdict.allowed
    assert GATE_INSTRUMENT in verdict.gates.failed_gates


def test_a_premium_above_the_affordability_ceiling_is_rejected(guard, equity):
    expiry = SESSION + timedelta(days=150)
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument=equity,
        is_option=True,
        nav=NAV,
        expiry=expiry,
        premium_per_share=10_000.0,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
        mandatory_close_date=_close_date(guard, expiry),
    )
    assert not verdict.allowed
    assert "price_ceiling" in verdict.gates.failed_gates


def test_a_premium_its_own_costs_would_dominate_is_rejected(guard, equity):
    """Below the absolute floor, round-trip commission outweighs the position."""
    expiry = SESSION + timedelta(days=150)
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument=equity,
        is_option=True,
        nav=NAV,
        expiry=expiry,
        premium_per_share=0.01,
        contracts=1,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
        mandatory_close_date=_close_date(guard, expiry),
    )
    assert not verdict.allowed
    assert GATE_POSITION_SIZE in verdict.gates.failed_gates


def test_an_illiquid_contract_is_rejected(guard, equity):
    expiry = SESSION + timedelta(days=150)
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument=equity,
        is_option=True,
        nav=NAV,
        expiry=expiry,
        premium_per_share=2.5,
        spread_pct_of_mid=0.90,
        open_interest=1,
        volume=0,
        mandatory_close_date=_close_date(guard, expiry),
    )
    assert not verdict.allowed
    assert {"spread", "open_interest"} & set(verdict.gates.failed_gates)


def test_a_missing_mandatory_close_date_is_rejected(guard, equity):
    """Setting it at entry is what makes the exit controlled, not inherited."""
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument=equity,
        is_option=True,
        nav=NAV,
        expiry=SESSION + timedelta(days=150),
        premium_per_share=2.5,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
    )
    assert not verdict.allowed
    assert GATE_MANDATORY_CLOSE in verdict.gates.failed_gates


def test_a_close_date_disagreeing_with_the_calendar_is_rejected(guard, equity):
    expiry = SESSION + timedelta(days=150)
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument=equity,
        is_option=True,
        nav=NAV,
        expiry=expiry,
        premium_per_share=2.5,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
        mandatory_close_date=expiry - timedelta(days=1),
    )
    assert not verdict.allowed
    assert GATE_MANDATORY_CLOSE in verdict.gates.failed_gates


def test_the_concurrency_cap_closes(seeded_store, config, equity):
    """Splitting does not reduce the penalty, it raises the odds of paying it."""
    cap = config.require_int("option_sleeve.max_concurrent_positions")
    for index in range(cap):
        open_position(
            seeded_store,
            contract_id=1,
            opened_at="2026-01-01",
            entry_price=2.5 + index,
            thesis_text=THESIS,
            invalidation_trigger=INVALIDATION,
        )
    guard = EntryGuard(seeded_store, config)
    verdict = guard.check_entry(as_of=SESSION, instrument=equity, is_option=False, nav=NAV)
    assert not verdict.allowed
    assert GATE_CONCURRENCY in verdict.gates.failed_gates


def test_adding_to_an_open_position_is_rejected_without_a_reunderwrite(
    seeded_store, config, equity
):
    """Averaging down is the single most destructive pattern available here."""
    open_position(
        seeded_store,
        contract_id=1,
        opened_at="2026-01-01",
        entry_price=2.5,
        thesis_text=THESIS,
        invalidation_trigger=INVALIDATION,
    )
    guard = EntryGuard(seeded_store, config)
    verdict = guard.check_entry(as_of=SESSION, instrument=equity, is_option=False, nav=NAV)
    assert not verdict.allowed
    assert "averaging down is prohibited" in (verdict.reason or "")


def test_a_logged_dated_catalyst_unlocks_the_add(seeded_store, config, equity):
    from obliskiq.store.ledger import append_reunderwrite

    position_id = open_position(
        seeded_store,
        contract_id=1,
        opened_at="2026-01-01",
        entry_price=2.5,
        thesis_text=THESIS,
        invalidation_trigger=INVALIDATION,
    )
    append_reunderwrite(
        seeded_store,
        position_id,
        dated_at="2026-01-15",
        catalyst="antitrust ruling",
        catalyst_date="2026-03-02",
        note="new dated catalyst that did not exist at entry",
    )
    guard = EntryGuard(seeded_store, config)
    verdict = guard.check_entry(as_of=SESSION, instrument=equity, is_option=False, nav=NAV)
    assert verdict.allowed, verdict.reason


def test_an_expiry_before_the_catalyst_buffer_is_rejected(guard, equity):
    """Catalysts slip and post-event repricing takes days."""
    expiry = SESSION + timedelta(days=95)
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument=equity,
        is_option=True,
        nav=NAV,
        expiry=expiry,
        catalyst_date=SESSION + timedelta(days=90),
        premium_per_share=2.5,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
        mandatory_close_date=_close_date(guard, expiry),
    )
    assert not verdict.allowed
    assert "catalyst_buffer" in verdict.gates.failed_gates


def test_a_gate_with_no_input_is_reported_untested_not_passed(guard, equity):
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument=equity,
        is_option=True,
        nav=NAV,
        premium_per_share=2.5,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
    )
    assert GATE_TENOR in verdict.untested
    assert GATE_TENOR not in verdict.checked


# ── the sleeve budget is a shared pool ──


def _commit(store, config, *, premium_per_share: float, contracts: int) -> float:
    """An open position that has already spent part of the sleeve."""
    position_id = open_position(
        store,
        contract_id=1,
        opened_at="2026-01-01",
        entry_price=premium_per_share,
        thesis_text=THESIS,
        invalidation_trigger=INVALIDATION,
    )
    multiplier = config.require_int("instruments.default_option_multiplier")
    committed = premium_per_share * multiplier * contracts
    PenaltyLedger(store, config).book_premium_committed(
        premium_paid=committed, position_id=position_id
    )
    return committed


def test_a_second_entry_is_sized_against_what_the_first_one_left(seeded_store, config):
    """The defect this session exists to close, at the gate that writes the ledger.

    An open position committing most of the sleeve must leave the next entry the
    REMAINDER. Before the pool existed, the guard read open_positions() only to
    count them, so max_concurrent_positions entries could each be sized against
    the whole sleeve — a "hard cap, % of current NAV" that capped nothing.
    """
    budget_pct = config.require_float("option_sleeve.budget_pct")
    multiplier = config.require_int("instruments.default_option_multiplier")
    cap = config.require_int("option_sleeve.max_concurrent_positions")
    gross = budget_pct * NAV

    # Sixty percent of the sleeve, spent and still open.
    per_share = gross * 0.6 / multiplier
    _commit(seeded_store, config, premium_per_share=per_share, contracts=1)

    guard = EntryGuard(seeded_store, config)
    expiry = SESSION + timedelta(days=150)
    second = {**_OTHER_INSTRUMENT}
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument=second,
        is_option=True,
        nav=NAV,
        expiry=expiry,
        premium_per_share=per_share,
        contracts=1,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
        mandatory_close_date=_close_date(guard, expiry),
    )

    assert verdict.open_positions < cap, "concurrency is NOT what should refuse this"
    assert GATE_CONCURRENCY not in verdict.gates.failed_gates
    assert not verdict.allowed, "two positions at 60% of the sleeve is 120% of the sleeve"
    assert GATE_PRICE_CEILING in verdict.gates.failed_gates
    assert verdict.ceiling is not None
    assert verdict.ceiling.available_budget == pytest.approx(gross * 0.4)
    assert verdict.ceiling.committed_premium == pytest.approx(gross * 0.6)


def test_what_the_first_entry_left_is_still_spendable(seeded_store, config):
    """The pool narrows the next entry; it does not close the sleeve."""
    budget_pct = config.require_float("option_sleeve.budget_pct")
    multiplier = config.require_int("instruments.default_option_multiplier")
    gross = budget_pct * NAV

    _commit(seeded_store, config, premium_per_share=gross * 0.6 / multiplier, contracts=1)

    guard = EntryGuard(seeded_store, config)
    expiry = SESSION + timedelta(days=150)
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument={**_OTHER_INSTRUMENT},
        is_option=True,
        nav=NAV,
        expiry=expiry,
        premium_per_share=gross * 0.3 / multiplier,
        contracts=1,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
        mandatory_close_date=_close_date(guard, expiry),
    )
    assert verdict.allowed, verdict.reason


# ── the three disciplines that used to be skill-only ──
#
# Each existed in config and was enforced nowhere: option_sleeve.min_payoff_ratio
# was read only inside the zombie test's derived target, never as an entry gate;
# discipline.entry_top_decile_requires_catalyst was read by no code at all; and
# the theta budget ran at REVIEW time, which is the wrong end of the trade.
# CLAUDE.md: the skill must never be the only thing between a bad trade and the
# ledger.


def _option(guard, equity, **overrides):
    """An option entry that clears everything except what a test breaks."""
    expiry = SESSION + timedelta(days=150)
    arguments: dict[str, object] = {
        "as_of": SESSION,
        "instrument": equity,
        "is_option": True,
        "nav": NAV,
        "expiry": expiry,
        "premium_per_share": 2.0,
        "contracts": 1,
        "spread_pct_of_mid": 0.02,
        "open_interest": 5_000,
        "volume": 100,
        "mandatory_close_date": _close_date(guard, expiry),
        "strike": 100.0,
        "is_call": True,
        "target": 106.0,
        "entry_range_pctile": 0.30,
    }
    arguments.update(overrides)
    return guard.check_entry(**arguments)  # type: ignore[arg-type]


# ── payoff ratio ──


def test_a_target_below_the_payoff_ratio_is_rejected(guard, equity, config):
    """The true cost of a loss here is the premium times the penalty multiple, so
    a symmetric payoff loses before it is even wrong."""
    ratio = config.require_float("option_sleeve.min_payoff_ratio")
    premium = 2.0
    # A target paying barely more than the premium back.
    verdict = _option(guard, equity, target=100.0 + premium * (ratio - 1.0))

    assert not verdict.allowed
    assert GATE_PAYOFF_RATIO in verdict.gates.failed_gates
    assert "option_sleeve.min_payoff_ratio" in (verdict.reason or "")


def test_a_target_clearing_the_payoff_ratio_passes(guard, equity, config):
    ratio = config.require_float("option_sleeve.min_payoff_ratio")
    premium = 2.0
    verdict = _option(guard, equity, target=100.0 + premium * ratio)

    assert GATE_PAYOFF_RATIO not in verdict.gates.failed_gates


def test_the_payoff_gate_agrees_with_the_target_the_engine_derives(
    guard, equity, seeded_store, config
):
    """RiskEngine.target_level derives its level as strike + ratio * premium. A
    gate that would reject the target the engine itself derives is worse than no
    gate: the two would disagree about the same trade."""
    from obliskiq.compute.risk_engine import RiskEngine

    engine = RiskEngine(seeded_store, config)
    derived = engine.target_level(strike=100.0, premium_paid=2.0, is_call=True, stated=None)
    verdict = _option(guard, equity, target=derived.level)

    assert GATE_PAYOFF_RATIO not in verdict.gates.failed_gates


def test_a_put_is_measured_on_its_own_side_of_the_strike(guard, equity, config):
    """Intrinsic runs the other way for a put, and a gate that forgot would
    reject every put ever written."""
    ratio = config.require_float("option_sleeve.min_payoff_ratio")
    verdict = _option(guard, equity, is_call=False, target=100.0 - 2.0 * ratio, strike=100.0)
    assert GATE_PAYOFF_RATIO not in verdict.gates.failed_gates


# ── the top-decile catalyst rule ──


def test_a_top_decile_entry_without_a_catalyst_is_rejected(guard, equity, config):
    """The most expensive pattern in this account. ONDS was entered near 85% of
    its range and IREN near 75%, both into strength without a dated catalyst."""
    if not config.require_bool("discipline.entry_top_decile_requires_catalyst"):
        pytest.skip("discipline.entry_top_decile_requires_catalyst is off in this config")
    pctile = config.require_float("discipline.entry_top_decile_pctile")
    verdict = _option(guard, equity, entry_range_pctile=pctile + 0.05, catalyst_date=None)

    assert not verdict.allowed
    assert GATE_ENTRY_RANGE in verdict.gates.failed_gates
    assert "beyond momentum" in (verdict.reason or "")


def test_a_top_decile_entry_with_a_dated_catalyst_passes(guard, equity, config):
    pctile = config.require_float("discipline.entry_top_decile_pctile")
    verdict = _option(
        guard,
        equity,
        entry_range_pctile=pctile + 0.05,
        catalyst_date=SESSION + timedelta(days=40),
    )
    assert GATE_ENTRY_RANGE not in verdict.gates.failed_gates


def test_an_entry_below_the_top_decile_needs_no_catalyst(guard, equity, config):
    pctile = config.require_float("discipline.entry_top_decile_pctile")
    verdict = _option(guard, equity, entry_range_pctile=pctile - 0.30, catalyst_date=None)
    assert GATE_ENTRY_RANGE not in verdict.gates.failed_gates


def test_the_range_rule_binds_on_equity_too(guard, equity, config):
    """Buying strength without a catalyst is not an option-only failure. Both of
    the positions this rule exists for were equity."""
    pctile = config.require_float("discipline.entry_top_decile_pctile")
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument=equity,
        is_option=False,
        nav=NAV,
        entry_range_pctile=pctile + 0.05,
    )
    assert not verdict.allowed
    assert GATE_ENTRY_RANGE in verdict.gates.failed_gates


def test_an_entry_that_never_said_where_in_the_range_it_sits_is_untested_not_passed(guard, equity):
    """The guard reports it; log_entry refuses it. The distinction matters: the
    guard is also called for a dry check, where refusing would be wrong."""
    verdict = _option(guard, equity, entry_range_pctile=None)
    assert GATE_ENTRY_RANGE in verdict.untested
    assert GATE_ENTRY_RANGE not in verdict.gates.failed_gates


# ── the theta budget ──


def test_the_theta_budget_is_untested_when_the_store_has_no_chain(guard, equity, config):
    """A store-side absence, not a caller-side one. Sigma comes off a collected
    smile, and refusing every entry on a young store would make the tool
    unusable for the reason it exists."""
    if not config.require_bool("tenor.require_theta_budget_check"):
        pytest.skip("tenor.require_theta_budget_check is off in this config")
    verdict = _option(guard, equity, spot=100.0, expected_resolution_days=140)

    assert GATE_THETA_BUDGET in verdict.untested
    assert GATE_THETA_BUDGET not in verdict.gates.failed_gates


def test_a_tenor_that_decays_below_the_floor_is_rejected(guard, equity, seeded_store, config):
    """zombie_test.min_recoverable_pct_of_premium, applied at ENTRY. Extrinsic
    value scales roughly with the square root of time, so a long hold burns a
    large share of it before the thesis has had a chance to work."""
    if not config.require_bool("tenor.require_theta_budget_check"):
        pytest.skip("tenor.require_theta_budget_check is off in this config")
    _seed_smile(seeded_store, spot=100.0)
    _seed_rate(seeded_store)

    # Far out of the money, held nearly to expiry with nothing else moving.
    verdict = _option(
        guard,
        equity,
        strike=130.0,
        target=130.0 + 2.0 * config.require_float("option_sleeve.min_payoff_ratio"),
        spot=100.0,
        expected_resolution_days=145,
    )

    assert not verdict.allowed
    assert GATE_THETA_BUDGET in verdict.gates.failed_gates
    assert "tenor.require_theta_budget_check" in (verdict.reason or "")
    assert verdict.theta_budget is not None
    assert not verdict.theta_budget.passes


def test_a_tenor_with_room_passes_and_reports_what_survives(guard, equity, seeded_store, config):
    """Returned whether it passes or fails: a tenor that only just clears is
    worth seeing."""
    if not config.require_bool("tenor.require_theta_budget_check"):
        pytest.skip("tenor.require_theta_budget_check is off in this config")
    _seed_smile(seeded_store, spot=100.0)
    _seed_rate(seeded_store)

    verdict = _option(guard, equity, spot=100.0, expected_resolution_days=20)

    assert GATE_THETA_BUDGET not in verdict.gates.failed_gates
    assert verdict.theta_budget is not None
    assert verdict.theta_budget.passes


def _seed_smile(store, *, spot: float) -> None:
    """A chain clean enough to clear smile hygiene, around ``spot``."""
    strikes = [(spot * factor, 0.34) for factor in (0.8, 0.9, 1.0, 1.1, 1.2, 1.3)]
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, right, "
            "bid, ask, last, iv, delta, open_interest, volume, source, market_data_status) "
            "VALUES (?, 1, ?, ?, 'C', 1.98, 2.02, 2.0, ?, 0.40, 5000, 120, 'cp_api', "
            "'REALTIME')",
            [
                (
                    SESSION.isoformat(),
                    (SESSION + timedelta(days=150)).isoformat(),
                    strike,
                    iv,
                )
                for strike, iv in strikes
            ],
        )


def _seed_rate(store) -> None:
    """A collected risk-free rate, so the theta budget has one to discount at."""
    from obliskiq.config import load_config

    config = load_config("config.yaml")
    base = config.require_str("account.base_currency")
    series_id = config.get("macro.risk_free_rate.series_by_currency")[base]
    from obliskiq.macro.rates import SOURCE

    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO macro (series_id, source, reference_date, release_date, value, "
            "frequency, units) VALUES (?, ?, ?, ?, 3.0, 'daily', 'percent')",
            (series_id, SOURCE, SESSION.isoformat(), SESSION.isoformat()),
        )


# ── the gates that were declared and not applied ──


def _usd_instrument() -> dict[str, object]:
    return {
        "contract_id": 3,
        "symbol": "USDNAME",
        "asset_class": "STK",
        "currency": "USD",
        "tradable": 1,
        "priips_restricted": 0,
    }


def _seed_fx(store, *, rate: float = 1.10, as_of: str = "2026-01-15") -> None:
    """One ECB EUR/USD observation, released on its own reference date.

    The convention is units of the foreign currency PER EURO, so a premium in
    USD becomes base by DIVIDING by this — which BaseCurrencyRates does once, in
    one place, so no call site has to remember the direction.
    """
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO macro (series_id, source, reference_date, release_date, value) "
            "VALUES (?, ?, ?, ?, ?)",
            ("EURUSD", "ecb", as_of, as_of, rate),
        )


def test_a_position_size_gate_with_no_nav_is_reported_untested(guard, equity):
    """It used to appear in NEITHER list, which reads as 'not applicable'."""
    expiry = SESSION + timedelta(days=150)
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument=equity,
        is_option=True,
        nav=None,  # and nav_history is empty in this fixture
        expiry=expiry,
        premium_per_share=2.5,
        contracts=1,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
        mandatory_close_date=_close_date(guard, expiry),
    )
    assert GATE_PRICE_CEILING in verdict.untested
    assert GATE_POSITION_SIZE in verdict.untested
    assert GATE_POSITION_SIZE not in verdict.checked


def test_the_mandatory_close_gate_is_reported_when_there_is_no_expiry(guard, equity):
    """Without an expiry the calendar has nothing to count back from.

    The gate used to vanish from both lists, so an option entry omitting `expiry`
    was logged with no close date computed at all — which is how one gets held
    into an auto-exercise. log_entry now requires expiry outright; other callers
    are told.
    """
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument=equity,
        is_option=True,
        nav=NAV,
        premium_per_share=2.5,
        contracts=1,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
    )
    assert GATE_MANDATORY_CLOSE in verdict.untested
    assert GATE_MANDATORY_CLOSE not in verdict.checked


def test_the_concurrency_cap_counts_the_option_sleeve_only(seeded_store, config, guard, equity):
    """Cash equity holdings do not consume slots in a sleeve they are not in."""
    max_concurrent = config.require_int("option_sleeve.max_concurrent_positions")
    for index in range(max_concurrent):
        open_position(
            seeded_store,
            contract_id=1,
            opened_at="2026-01-01",
            entry_price=2.5 + index,
            thesis_text=THESIS,
            invalidation_trigger=INVALIDATION,
            is_option=False,
            entry_currency="EUR",
        )

    expiry = SESSION + timedelta(days=150)
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument=_OTHER_INSTRUMENT,
        is_option=True,
        nav=NAV,
        expiry=expiry,
        premium_per_share=2.5,
        contracts=1,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
        mandatory_close_date=_close_date(guard, expiry),
    )
    assert GATE_CONCURRENCY not in verdict.gates.failed_gates, verdict.reason


def test_a_position_of_unknown_sleeve_still_counts_against_the_cap(seeded_store, config, guard):
    """A row written before the sleeve was recorded is counted, deliberately.

    The two failure directions are not symmetric: counting an equity row costs
    one refused entry the caller can argue with, while not counting an option row
    lets the sleeve fragment past its cap silently.
    """
    max_concurrent = config.require_int("option_sleeve.max_concurrent_positions")
    with seeded_store.transaction() as connection:
        for index in range(max_concurrent):
            connection.execute(
                "INSERT INTO position_ledger (contract_id, opened_at, entry_price, "
                "thesis_text, invalidation_trigger) VALUES (?, ?, ?, ?, ?)",
                (1, "2026-01-01", 2.5 + index, THESIS, INVALIDATION),
            )

    expiry = SESSION + timedelta(days=150)
    verdict = guard.check_entry(
        as_of=SESSION,
        instrument=_OTHER_INSTRUMENT,
        is_option=True,
        nav=NAV,
        expiry=expiry,
        premium_per_share=2.5,
        contracts=1,
        mandatory_close_date=_close_date(guard, expiry),
    )
    assert GATE_CONCURRENCY in verdict.gates.failed_gates


def test_a_foreign_premium_is_converted_before_it_is_compared(seeded_store, config, guard):
    """The gate reads the rate from the store, and reports untested without one."""
    expiry = SESSION + timedelta(days=150)
    usd = _usd_instrument()

    before = guard.check_entry(
        as_of=SESSION,
        instrument=usd,
        is_option=True,
        nav=NAV,
        expiry=expiry,
        premium_per_share=2.5,
        contracts=1,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
        mandatory_close_date=_close_date(guard, expiry),
    )
    assert GATE_PRICE_CEILING in before.untested, "no rate: the comparison must not happen"

    _seed_fx(seeded_store)
    # A fresh guard: BaseCurrencyRates caches a lookup that came back empty.
    after = EntryGuard(seeded_store, config).check_entry(
        as_of=SESSION,
        instrument=usd,
        is_option=True,
        nav=NAV,
        expiry=expiry,
        premium_per_share=2.5,
        contracts=1,
        spread_pct_of_mid=0.02,
        open_interest=10_000,
        volume=500,
        mandatory_close_date=_close_date(guard, expiry),
    )
    assert GATE_PRICE_CEILING in after.checked
    assert GATE_PRICE_CEILING not in after.untested


def test_the_converter_returns_the_rate_it_used(seeded_store, config):
    """What the commitment is booked at has to be auditable, not reconstructed."""
    _seed_fx(seeded_store, rate=1.25)
    guard = EntryGuard(seeded_store, config)

    base, conversion = guard.convert_to_base(250.0, currency="USD", as_of=SESSION)
    assert base == pytest.approx(200.0), "units of USD per EUR: divide, never multiply"
    assert conversion is not None
    assert conversion.currency == "USD"
    assert conversion.quote_amount == pytest.approx(250.0)

    already_base = guard.convert_to_base(250.0, currency="EUR", as_of=SESSION)
    assert already_base == (250.0, None), "nothing to record when nothing was converted"

    assert guard.convert_to_base(250.0, currency="JPY", as_of=SESSION) is None
    assert guard.convert_to_base(250.0, currency=None, as_of=SESSION) is None
