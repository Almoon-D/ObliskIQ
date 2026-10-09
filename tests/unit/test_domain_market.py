"""Bars, snapshots, option chain rows and curve points.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import pytest
from pydantic import ValidationError

from obliskiq.domain.enums import MarketDataStatus, OptionRight


def test_a_bar_needs_a_close_but_not_the_rest(make_bar):
    bar = make_bar(open=None, high=None, low=None, volume=None)
    assert bar.close == 100.5
    with pytest.raises(ValidationError):
        make_bar(close=None)


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
def test_a_bar_has_no_negative_prices_or_volume(make_bar, field):
    with pytest.raises(ValidationError):
        make_bar(**{field: -1.0})


def test_a_snapshot_keeps_vendor_and_solved_vol_apart(make_snapshot):
    snapshot = make_snapshot(
        implied_vol=None,
        implied_vol_source=None,
        implied_vol_solved=0.3,
        implied_vol_solved_source="solved_bsm_european",
    )
    assert snapshot.implied_vol is None
    assert snapshot.implied_vol_solved == 0.3


def test_a_solved_vol_names_how_it_was_solved(make_snapshot):
    with pytest.raises(ValidationError, match="implied_vol_solved"):
        make_snapshot(implied_vol_solved=0.3)
    with pytest.raises(ValidationError, match="implied_vol_solved"):
        make_snapshot(implied_vol_solved_source="solved_bsm_european")


def test_a_dividend_yield_names_its_basis(make_snapshot):
    assert (
        make_snapshot(dividend_yield=0.0, dividend_yield_basis="trailing_12m").dividend_yield == 0
    )
    with pytest.raises(ValidationError, match="dividend_yield"):
        make_snapshot(dividend_yield=0.01)
    with pytest.raises(ValidationError, match="dividend_yield"):
        make_snapshot(dividend_yield_basis="trailing_12m")


def test_a_missing_status_is_not_realtime(make_snapshot):
    snapshot = make_snapshot(market_data_status=None, market_data_raw="DPB")
    assert snapshot.market_data_status is None
    assert snapshot.market_data_raw == "DPB"


def test_a_quote_delay_is_not_negative(make_snapshot):
    with pytest.raises(ValidationError):
        make_snapshot(quote_delay_seconds=-1)


def test_a_chain_row_mid_and_spread(make_chain_row):
    row = make_chain_row(bid=4.0, ask=6.0)
    assert row.mid == 5.0
    assert row.spread_pct_of_mid == pytest.approx(0.4)


def test_a_missing_side_has_no_mid_and_no_spread(make_chain_row):
    row = make_chain_row(bid=None)
    assert row.mid is None
    assert row.spread_pct_of_mid is None


def test_a_zero_mid_has_no_spread(make_chain_row):
    assert make_chain_row(bid=0.0, ask=0.0).spread_pct_of_mid is None


def test_a_crossed_book_is_a_negative_spread_not_a_missing_one(make_chain_row):
    row = make_chain_row(bid=6.0, ask=4.0)
    assert row.spread_pct_of_mid is not None
    assert row.spread_pct_of_mid < 0


def test_a_put_is_a_put(make_chain_row):
    assert make_chain_row(right="P").right is OptionRight.PUT


def test_an_unknown_right_is_refused(make_chain_row):
    with pytest.raises(ValidationError):
        make_chain_row(right="X")


@pytest.mark.parametrize("delta", [-1.01, 1.5])
def test_a_delta_outside_one_is_refused(make_chain_row, delta):
    with pytest.raises(ValidationError, match="delta"):
        make_chain_row(delta=delta)


def test_the_strike_is_positive(make_chain_row):
    with pytest.raises(ValidationError):
        make_chain_row(strike=0.0)


def test_the_strike_band_sigma_names_its_source(make_chain_row):
    assert make_chain_row(sigma_used=0.3, sigma_source="realised_30d").sigma_used == 0.3
    with pytest.raises(ValidationError, match="sigma_used"):
        make_chain_row(sigma_used=0.3)


def test_a_curve_point(make_curve_point):
    point = make_curve_point()
    assert point.market_data_status is MarketDataStatus.DELAYED
    assert point.quote_delay_seconds == 900
