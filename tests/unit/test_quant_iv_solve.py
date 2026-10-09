"""Choosing which contracts to solve, and solving them without claiming more than was found.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import pytest

from obliskiq.config import ObliskConfig
from obliskiq.domain.enums import OptionRight
from obliskiq.domain.market import ChainRow
from obliskiq.quant.bsm import BsmInputs, IvSolverInputs, bsm_price
from obliskiq.quant.iv_solve import (
    SOURCE_SOLVED_BSM_EUROPEAN,
    SolveInputs,
    SolveReport,
    select_contracts,
    solve_rows,
)
from obliskiq.quant.smile import Smile, SmileFilters

SESSION = date(2026, 9, 2)
SPOT = 100.0
DTE = 120  # inside numerics.implied_vol.solve's band
EXPIRY = SESSION + timedelta(days=DTE)
RATE = 0.04
TRUE_SIGMA = 0.24
STRIKES = (88.0, 92.0, 96.0, 100.0, 104.0, 108.0, 112.0)


@pytest.fixture
def bsm(shipped_settings: ObliskConfig) -> BsmInputs:
    return BsmInputs.from_config(shipped_settings)


@pytest.fixture
def solver(shipped_settings: ObliskConfig) -> IvSolverInputs:
    return IvSolverInputs.from_config(shipped_settings)


@pytest.fixture
def inputs(shipped_settings: ObliskConfig) -> SolveInputs:
    return SolveInputs.from_config(shipped_settings)


@pytest.fixture
def vendor_chain(
    make_chain_row: Callable[..., ChainRow], bsm: BsmInputs
) -> Callable[..., list[ChainRow]]:
    """A chain priced from a known sigma and carrying no implied vol: the vendor case."""

    def build(
        sigma: float = TRUE_SIGMA, expiry: date = EXPIRY, strikes: tuple[float, ...] = STRIKES
    ) -> list[ChainRow]:
        rows = []
        for strike in strikes:
            is_call = strike >= SPOT
            fair = float(
                bsm_price(SPOT, strike, (expiry - SESSION).days, sigma, RATE, 0.0, is_call, bsm)
            )
            rows.append(
                make_chain_row(
                    snapshot_date=SESSION,
                    expiry=expiry,
                    strike=strike,
                    right=OptionRight.CALL if is_call else OptionRight.PUT,
                    bid=fair * 0.999,
                    ask=fair * 1.001,
                    last=fair,
                    iv=None,
                    delta=None,
                    open_interest=5_000,
                    volume=1_000,
                    market_data_status=None,
                    market_data_raw=None,
                )
            )
        return rows

    return build


def solve(rows: list[ChainRow], bsm: BsmInputs, solver: IvSolverInputs, **kw: Any):  # type: ignore[no-untyped-def]
    return solve_rows(
        rows,
        spot=SPOT,
        session=SESSION,
        rate=RATE,
        dividend_yield=0.0,
        bsm=bsm,
        solver=solver,
        **kw,
    )


# ── choosing contracts ──


def test_the_solver_recovers_the_sigma_the_chain_was_priced_from(vendor_chain, bsm, solver):
    result = solve(vendor_chain(), bsm, solver)
    assert result.converged == result.attempted == len(STRIKES)
    assert all(row.iv == pytest.approx(TRUE_SIGMA, abs=2e-3) for row in result.rows)
    assert result.reasons == {"ok": len(STRIKES)}


def test_a_solved_row_carries_the_model_delta_and_keeps_everything_else(vendor_chain, bsm, solver):
    original = {row.strike: row for row in vendor_chain()}
    result = solve(list(original.values()), bsm, solver)
    for row in result.rows:
        assert row.delta is not None
        assert -1.0 <= row.delta <= 1.0
        assert (row.delta < 0) is (row.right is OptionRight.PUT)
        assert row.model_copy(update={"iv": None, "delta": None}) == original[row.strike]


def test_the_solved_rows_make_a_smile_whose_atm_matches_the_input(
    vendor_chain, bsm, solver, shipped_settings
):
    result = solve(vendor_chain(), bsm, solver)
    smile = Smile.from_rows(
        result.rows, spot=SPOT, filters=SmileFilters.from_config(shipped_settings)
    )
    assert smile is not None
    assert smile.hygiene.applied
    assert smile.atm().iv == pytest.approx(TRUE_SIGMA, abs=5e-3)


def test_one_expiry_is_chosen_the_nearest_to_the_middle_of_the_band(vendor_chain, inputs):
    middle = (inputs.min_dte + inputs.max_dte) // 2
    near_middle = SESSION + timedelta(days=middle)
    edge = SESSION + timedelta(days=inputs.min_dte)
    rows = vendor_chain(expiry=near_middle) + vendor_chain(expiry=edge)
    chosen = select_contracts(rows, spot=SPOT, session=SESSION, inputs=inputs)
    assert {row.expiry for row in chosen} == {near_middle}
    assert len(chosen) == len(STRIKES)


def test_an_expiry_outside_the_dte_band_is_not_solved(vendor_chain, inputs):
    """Outside [min_dte, max_dte] the European approximation is not tolerable, and labelling a
    value does not make it acceptable."""
    too_soon = SESSION + timedelta(days=inputs.min_dte - 1)
    too_late = SESSION + timedelta(days=inputs.max_dte + 1)
    rows = vendor_chain(expiry=too_soon) + vendor_chain(expiry=too_late)
    assert select_contracts(rows, spot=SPOT, session=SESSION, inputs=inputs) == []
    edges = [SESSION + timedelta(days=inputs.min_dte), SESSION + timedelta(days=inputs.max_dte)]
    for edge in edges:
        assert select_contracts(
            vendor_chain(expiry=edge), spot=SPOT, session=SESSION, inputs=inputs
        )


def test_strikes_outside_the_moneyness_band_are_not_solved(vendor_chain, inputs):
    wide = (60.0, 100.0, 140.0)
    chosen = select_contracts(vendor_chain(strikes=wide), spot=SPOT, session=SESSION, inputs=inputs)
    assert [row.strike for row in chosen] == [100.0]
    edge = SPOT * (1.0 + inputs.moneyness_band)
    on_the_edge = vendor_chain(strikes=(edge,))
    assert len(select_contracts(on_the_edge, spot=SPOT, session=SESSION, inputs=inputs)) == 1


def test_a_contract_with_no_positive_mid_is_not_a_price_to_invert(
    make_chain_row, vendor_chain, inputs
):
    rows = vendor_chain()
    rows[0] = make_chain_row(expiry=EXPIRY, strike=88.0, bid=None, ask=None, iv=None, delta=None)
    rows[1] = make_chain_row(expiry=EXPIRY, strike=92.0, bid=0.0, ask=0.0, iv=None, delta=None)
    chosen = select_contracts(rows, spot=SPOT, session=SESSION, inputs=inputs)
    assert {row.strike for row in chosen} == set(STRIKES) - {88.0, 92.0}


# ── what is not found is counted, never invented ──


def test_a_book_below_intrinsic_converges_nowhere_and_says_why(make_chain_row, bsm, solver, inputs):
    row = make_chain_row(
        expiry=EXPIRY,
        strike=90.0,
        right=OptionRight.CALL,
        bid=0.005,
        ask=0.015,
        iv=None,
        delta=None,
    )
    result = solve([row], bsm, solver)
    assert result.rows == ()
    assert (result.converged, result.attempted) == (0, 1)
    assert result.reasons == {"below_intrinsic": 1}


def test_a_row_with_no_mid_is_counted_as_attempted_and_not_converged(make_chain_row, bsm, solver):
    row = make_chain_row(expiry=EXPIRY, strike=100.0, bid=None, ask=None, iv=None, delta=None)
    result = solve([row], bsm, solver)
    assert result.rows == ()
    assert result.reasons == {"non_positive_price": 1}


# ── the run report ──


def test_a_run_past_the_non_convergence_ceiling_is_degraded(make_chain_row, bsm, solver, inputs):
    report = SolveReport(as_of=SESSION)
    bad = make_chain_row(
        expiry=EXPIRY,
        strike=90.0,
        right=OptionRight.CALL,
        bid=0.005,
        ask=0.015,
        iv=None,
        delta=None,
    )
    report.add(solve([bad], bsm, solver))
    assert report.non_converged_fraction == 1.0
    assert report.is_degraded(inputs)
    assert "did not" in report.summary(inputs)
    assert report.reasons == {"below_intrinsic": 1}


def test_a_run_within_the_ceiling_is_not_degraded(vendor_chain, bsm, solver, inputs):
    report = SolveReport(as_of=SESSION)
    report.add(solve(vendor_chain(), bsm, solver))
    report.solved += 1
    assert report.non_converged_fraction == 0.0
    assert not report.is_degraded(inputs)
    assert report.summary(inputs).startswith("1 underlying(s) solved; 7 of 7 contracts converged")


def test_a_run_with_nothing_to_attempt_is_not_degraded(inputs):
    """An empty store is a young store, not a broken run."""
    report = SolveReport(as_of=SESSION)
    assert report.attempted_contracts == 0
    assert report.non_converged_fraction == 0.0
    assert not report.is_degraded(inputs)
    assert report.summary(inputs) == "no contract was in a solvable band on any underlying"


def test_skips_and_reasons_accumulate_across_underlyings():
    report = SolveReport(as_of=SESSION)
    report.skip("no_chain")
    report.skip("no_chain")
    report.skip("no_rate")
    assert report.skipped == {"no_chain": 2, "no_rate": 1}


def test_the_solved_value_is_labelled_european():
    assert "european" in SOURCE_SOLVED_BSM_EUROPEAN


def test_the_inputs_come_from_config(shipped_settings, inputs):
    solve_config = shipped_settings.numerics.implied_vol.solve
    assert (inputs.min_dte, inputs.max_dte) == (solve_config.min_dte, solve_config.max_dte)
    assert inputs.moneyness_band == solve_config.moneyness_band
    assert inputs.max_non_converged_fraction == solve_config.max_non_converged_fraction
