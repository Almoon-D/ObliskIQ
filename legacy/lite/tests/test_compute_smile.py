"""The smile, and the reason it exists: N(d2) must not be computed on ATM vol.

Constant-vol Black-Scholes understates the terminal probability on out-of-the-
money strikes. The zombie test fires when N(d2) at the target falls below a
floor, so an ATM sigma biases it toward firing — a false positive that forces a
premature exit on a position that was still alive.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date

import pytest

from obliskiq.compute.frames import ChainRow
from obliskiq.compute.smile import (
    SOURCE_CLAMPED_HIGH,
    SOURCE_CLAMPED_LOW,
    SOURCE_INTERPOLATED,
    SOURCE_LISTED,
    Smile,
)
from obliskiq.quant.bsm import BsmInputs, probability_itm

SESSION = date(2026, 1, 15)
EXPIRY = date(2026, 6, 19)


def chain_row(strike, right, iv, *, open_interest=1_000, underlying_id=1, delta=None):
    return ChainRow(
        snapshot_date=SESSION,
        underlying_id=underlying_id,
        expiry=EXPIRY,
        strike=strike,
        right=right,
        bid=1.0,
        ask=1.2,
        last=1.1,
        iv=iv,
        delta=delta,
        open_interest=open_interest,
        volume=25,
        contract_id=None,
    )


#: A skewed chain: puts bid up, calls smiling out to the wing. Realistic, and
#: the ATM point is deliberately the lowest vol on the surface.
SKEWED = [
    chain_row(80.0, "P", 0.50),
    chain_row(90.0, "P", 0.38),
    chain_row(100.0, "C", 0.30),
    chain_row(110.0, "C", 0.36),
    chain_row(120.0, "C", 0.44),
]


def test_a_listed_strike_reads_its_own_implied_vol():
    smile = Smile.from_rows(SKEWED, spot=100.0)
    quote = smile.iv_at(110.0)
    assert quote.iv == 0.36
    assert quote.source == SOURCE_LISTED
    assert quote.is_measured


def test_a_strike_between_two_listed_ones_is_interpolated():
    smile = Smile.from_rows(SKEWED, spot=100.0)
    quote = smile.iv_at(105.0)
    assert quote.iv == pytest.approx(0.33)
    assert quote.source == SOURCE_INTERPOLATED
    assert quote.from_strikes == (100.0, 110.0)
    assert not quote.is_measured


def test_beyond_the_collected_wing_the_value_is_clamped_and_says_so():
    """The wing slope of a truncated chain is an artefact of where collection stopped."""
    smile = Smile.from_rows(SKEWED, spot=100.0)

    high = smile.iv_at(200.0)
    low = smile.iv_at(10.0)

    assert high.iv == 0.44
    assert high.source == SOURCE_CLAMPED_HIGH
    assert high.is_clamped
    assert low.iv == 0.50
    assert low.source == SOURCE_CLAMPED_LOW


def test_the_smile_takes_the_out_of_the_money_side_at_each_strike():
    """An in-the-money quote is nearly all intrinsic and carries the noisiest IV."""
    rows = [
        chain_row(90.0, "P", 0.38),
        chain_row(90.0, "C", 0.99),  # deep in the money: not the one to use
        chain_row(110.0, "C", 0.36),
        chain_row(110.0, "P", 0.99),  # deep in the money
    ]
    smile = Smile.from_rows(rows, spot=100.0)
    assert smile.iv_at(90.0).iv == 0.38
    assert smile.iv_at(110.0).iv == 0.36


def test_a_chain_with_no_implied_vol_produces_no_smile():
    """An absent smile is not an empty one; the caller must report it."""
    assert Smile.from_rows([chain_row(100.0, "C", None)], spot=100.0) is None
    assert Smile.from_rows([], spot=100.0) is None


def test_smile_iv_and_atm_iv_give_different_probabilities(config):
    """The whole reason zombie_test.prob_vol_source is not atm_iv.

    N(d2) at an out-of-the-money target, computed on the target strike's own vol
    against the at-the-money vol. On a skewed surface the two differ materially,
    and the ATM figure is the one that reads too low.
    """
    assert config.require_str("zombie_test.prob_vol_source") == "strike_smile_iv"

    params = BsmInputs.from_config(config)
    smile = Smile.from_rows(SKEWED, spot=100.0)
    target = 120.0

    atm_iv = smile.atm().iv
    strike_iv = smile.iv_at(target).iv
    assert strike_iv > atm_iv, "this fixture exists to have an upward call skew"

    on_atm = float(probability_itm(100.0, target, 155, atm_iv, 0.03, 0.0, True, params))
    on_smile = float(probability_itm(100.0, target, 155, strike_iv, 0.03, 0.0, True, params))

    assert on_smile > on_atm, (
        "constant-vol BS understates the probability on an OTM strike; that "
        "understatement is what produces a false zombie positive"
    )


def test_a_single_listed_strike_is_labelled_rather_than_interpolated():
    smile = Smile.from_rows([chain_row(100.0, "C", 0.30)], spot=100.0)
    quote = smile.iv_at(150.0)
    assert quote.iv == 0.30
    assert not quote.is_measured
    assert "single" in quote.source


def test_skew_is_the_difference_between_two_wings():
    smile = Smile.from_rows(SKEWED, spot=100.0)
    assert smile.skew(low_strike=90.0, high_strike=110.0) == pytest.approx(0.36 - 0.38)


# ── the risk reversal, which is what the grid's skew leaf reads ──
#
# ATM implied vol is symmetric: it says how much movement is priced and nothing
# about which way. The risk reversal is the direction, and it is defined by DELTA
# rather than by a moneyness — a fixed band means something different on a
# 15-vol name and a 90-vol one.

#: A textbook equity smile carrying the deltas the route served. Puts bid, calls
#: offered, so the risk reversal is NEGATIVE and the lower tail is the paid one.
DELTA_SKEWED = [
    chain_row(80.0, "P", 0.42, delta=-0.10),
    chain_row(90.0, "P", 0.36, delta=-0.30),
    chain_row(100.0, "C", 0.30, delta=0.50),
    chain_row(110.0, "C", 0.27, delta=0.22),
    chain_row(120.0, "C", 0.26, delta=0.08),
]


def test_the_risk_reversal_is_read_at_the_delta_asked_for_on_both_sides():
    smile = Smile.from_rows(DELTA_SKEWED, spot=100.0)
    quote = smile.risk_reversal(0.25)

    # 0.25 sits between the 0.10 and 0.30 puts and between the 0.22 and 0.50
    # calls, so both legs are interpolated rather than taken from a listed strike.
    assert quote.put_strike == pytest.approx(87.5)
    assert quote.call_strike == pytest.approx(108.928571)
    assert quote.value == pytest.approx(
        smile.skew(low_strike=quote.put_strike, high_strike=quote.call_strike)
    ), "the leaf reads the two strikes off the smile it already had; it recomputes nothing"
    assert quote.value < 0, "puts bid over calls is a put-side skew, and the sign says so"


def test_a_put_side_and_a_call_side_skew_come_back_with_opposite_signs():
    """The whole point of the leaf: which tail is bid, not how much vol there is."""
    puts_bid = Smile.from_rows(DELTA_SKEWED, spot=100.0).risk_reversal(0.25)
    mirrored = [
        chain_row(80.0, "P", 0.26, delta=-0.10),
        chain_row(90.0, "P", 0.27, delta=-0.30),
        chain_row(100.0, "C", 0.30, delta=0.50),
        chain_row(110.0, "C", 0.36, delta=0.22),
        chain_row(120.0, "C", 0.42, delta=0.08),
    ]
    calls_bid = Smile.from_rows(mirrored, spot=100.0).risk_reversal(0.25)

    assert puts_bid.value < 0 < calls_bid.value
    # Same ATM vol on both, which is exactly why the level cannot tell them apart.
    assert Smile.from_rows(DELTA_SKEWED, spot=100.0).atm().iv == pytest.approx(
        Smile.from_rows(mirrored, spot=100.0).atm().iv
    )


def test_a_band_that_cannot_reach_the_target_refuses_rather_than_substituting():
    """The nearest listed strike would publish a 45-delta reading under a
    25-delta name, and read as an ordinary number all the way to the output."""
    too_narrow = [
        chain_row(95.0, "P", 0.33, delta=-0.42),
        chain_row(98.0, "P", 0.31, delta=-0.46),
        chain_row(100.0, "C", 0.30, delta=0.50),
        chain_row(102.0, "C", 0.30, delta=0.45),
    ]
    smile = Smile.from_rows(too_narrow, spot=100.0)
    assert smile.risk_reversal(0.25) is None
    assert smile.atm() is not None, "the LEVEL is still measurable; only the direction is not"


def test_the_refusal_names_the_deltas_the_band_actually_reached():
    """`n/a` because the band is too narrow is a claim the reader can check."""
    smile = Smile.from_rows(
        [
            chain_row(95.0, "P", 0.33, delta=-0.42),
            chain_row(98.0, "P", 0.31, delta=-0.46),
            chain_row(100.0, "C", 0.30, delta=0.50),
            chain_row(102.0, "C", 0.30, delta=0.45),
        ],
        spot=100.0,
    )
    text = smile.delta_reach().describe()
    assert "0.42" in text and "0.46" in text
    assert "0.45" in text and "0.50" in text


def test_a_route_that_served_no_greeks_is_a_different_gap_from_a_thin_book():
    """SKEWED carries no deltas at all, which is the route not serving them.

    Reported separately because the remedy differs: a thin book wants a wider
    strike band, a missing greek wants a different collection route.
    """
    smile = Smile.from_rows(SKEWED, spot=100.0)
    assert smile.risk_reversal(0.25) is None
    reach = smile.delta_reach()
    assert reach.put_deltas is None and reach.call_deltas is None
    assert reach.points_without_delta == len(smile.points)
    assert "carried no delta" in reach.describe()


def test_the_delta_is_read_unsigned_so_a_put_and_a_call_are_comparable():
    """Routes serve a put's delta negative. A risk reversal compares two strikes
    the market prices as equally far out, which is a statement about magnitude."""
    smile = Smile.from_rows(DELTA_SKEWED, spot=100.0)
    put = next(point for point in smile.points if point.strike == 80.0)
    assert put.delta == -0.10, "the sign is kept verbatim from the route"
    assert put.abs_delta == pytest.approx(0.10)


def test_a_skew_is_only_ever_read_off_strikes_that_survived_hygiene():
    """A skew computed off a strike hygiene dropped is not a skew.

    Here the 0.30-delta put is the one a wide spread removes, which leaves the
    put side unable to bracket the target — so the leaf refuses rather than
    reaching past the dropped strike to the 0.10 wing.
    """
    from obliskiq.compute.smile import DROP_SPREAD, SmileFilters

    filters = SmileFilters(
        max_spread_pct=0.05,
        min_open_interest=100,
        require_session_volume=False,
        min_abs_delta=0.01,
        max_abs_delta=0.99,
        butterfly_max_iv_deviation=1.0,
        min_points=2,
        skew_delta=0.25,
    )

    def tight(strike, right, iv, delta, *, spread=0.04):
        """A strike whose spread is inside max_spread_pct unless asked otherwise."""
        row = chain_row(strike, right, iv, delta=delta)
        return ChainRow(**{**row.__dict__, "bid": 1.0, "ask": 1.0 + spread, "last": 1.0})

    rows = [
        tight(80.0, "P", 0.42, -0.10),
        tight(90.0, "P", 0.36, -0.30, spread=2.0),  # the only strike bracketing 0.25 below
        tight(100.0, "C", 0.30, 0.50),
        tight(110.0, "C", 0.27, 0.22),
        tight(120.0, "C", 0.26, 0.08),
    ]

    unfiltered = Smile.from_rows(rows, spot=100.0)
    filtered = Smile.from_rows(rows, spot=100.0, filters=filters)

    assert unfiltered.risk_reversal(0.25) is not None, "the strike is there if nobody filters"
    assert filtered.risk_reversal(0.25) is None, "and it is not there once hygiene has run"
    assert filtered.hygiene.dropped == {DROP_SPREAD: 1}, "the drop is counted, not silent"
    assert filtered.atm() is not None, "hygiene took the skew, not the whole smile"


def test_provenance_is_printable_for_the_audit_trail():
    smile = Smile.from_rows(SKEWED, spot=100.0)
    text = smile.iv_at(105.0).provenance()
    assert "interpolated" in text
    assert "100" in text and "110" in text


# ── hygiene ──
#
# A quote existing is not the same as a quote carrying information.
# zombie_test.prob_vol_source is strike_smile_iv, so a single bad implied vol on
# an illiquid strike propagates into N(d2) and out into an exit recommendation
# on a position that was still alive. These tests are the filter that stops it.


def clean_row(strike, right, iv, **overrides):
    """A strike that clears every hygiene test, so a test can break one at a time."""
    fields = {
        "snapshot_date": SESSION,
        "underlying_id": 1,
        "expiry": EXPIRY,
        "strike": strike,
        "right": right,
        "bid": 1.98,
        "ask": 2.02,  # 2% of mid, inside liquidity.max_spread_pct
        "last": 2.0,
        "iv": iv,
        "delta": 0.40,
        "open_interest": 5_000,
        "volume": 120,
        "contract_id": None,
        "source": "cp_api",
        "market_data_status": "REALTIME",
    }
    fields.update(overrides)
    return ChainRow(**fields)


def orderly_rows(**overrides):
    """Five strikes on a smooth smile, all clean."""
    return [
        clean_row(strike, "C" if strike >= 100.0 else "P", iv, **overrides)
        for strike, iv in ((80.0, 0.40), (90.0, 0.36), (100.0, 0.34), (110.0, 0.36), (120.0, 0.40))
    ]


@pytest.fixture()
def filters(config):
    from obliskiq.compute.smile import SmileFilters

    return SmileFilters.from_config(config)


def test_an_unfiltered_smile_says_so_rather_than_implying_it_passed(config):
    """`applied=False` is the difference between "nothing was dropped" and
    "nothing was tested". A caller must be able to tell them apart."""
    smile = Smile.from_rows(orderly_rows(), spot=100.0)
    assert smile is not None
    assert smile.hygiene.applied is False
    assert "UNFILTERED" in smile.hygiene.summary()


def test_a_clean_chain_survives_hygiene_intact(filters):
    smile = Smile.from_rows(orderly_rows(), spot=100.0, filters=filters)
    assert smile is not None
    assert len(smile) == 5
    assert smile.hygiene.applied is True
    assert smile.hygiene.dropped == {}
    assert smile.hygiene.kept == 5


def test_a_spread_wider_than_the_liquidity_gate_drops_the_strike(config, filters):
    """A vol solved off a mid nobody can trade at is a vol about nothing."""
    from obliskiq.compute.smile import DROP_SPREAD

    ceiling = config.require_float("liquidity.max_spread_pct")
    rows = orderly_rows()
    # Half a point either side of a 2.0 mid: 50% of mid, far beyond the gate.
    rows[2] = clean_row(100.0, "C", 0.34, bid=1.5, ask=2.5)
    assert (rows[2].spread_pct_of_mid or 0) > ceiling

    smile = Smile.from_rows(rows, spot=100.0, filters=filters)
    assert smile is not None
    assert 100.0 not in smile.strikes
    assert smile.hygiene.dropped == {DROP_SPREAD: 1}


def test_a_crossed_book_is_dropped_not_read_as_the_tightest_quote_on_the_chain(config, filters):
    """ask < bid makes spread_pct_of_mid NEGATIVE, which cleared every ceiling
    this test compares it against — `spread > max_spread_pct` is False for a
    negative number — so the worst quote on the chain read as tighter than
    every real one, survived hygiene, and its implied vol would have fed
    N(d2) on the strength of a broken print."""
    from obliskiq.compute.smile import DROP_CROSSED, DROP_OPEN_INTEREST

    rows = orderly_rows()
    rows[2] = clean_row(100.0, "C", 0.34, bid=5.0, ask=1.0)
    assert rows[2].spread_pct_of_mid < 0, "the raw signed fraction, not folded to None"

    smile = Smile.from_rows(rows, spot=100.0, filters=filters)
    assert smile is not None
    assert 100.0 not in smile.strikes
    assert smile.hygiene.dropped == {DROP_CROSSED: 1}
    # The open-interest gate is unaffected — this pins the row is dropped
    # for being crossed, not by coincidence of some other filter.
    assert DROP_OPEN_INTEREST not in smile.hygiene.dropped


def test_a_book_thinner_than_the_liquidity_gate_drops_the_strike(config, filters):
    from obliskiq.compute.smile import DROP_OPEN_INTEREST

    floor = config.require_int("liquidity.min_open_interest")
    rows = orderly_rows()
    rows[2] = clean_row(100.0, "C", 0.34, open_interest=floor - 1)

    smile = Smile.from_rows(rows, spot=100.0, filters=filters)
    assert smile is not None
    assert 100.0 not in smile.strikes
    assert smile.hygiene.dropped == {DROP_OPEN_INTEREST: 1}


def test_a_strike_that_did_not_trade_is_dropped_while_the_gate_requires_volume(config, filters):
    """liquidity.require_session_volume asks for the field PRESENT and positive.
    A NULL there cannot be told from a zero, and the gate exists to reject
    exactly the strike that did not trade."""
    from obliskiq.compute.smile import DROP_NO_VOLUME

    if not config.require_bool("liquidity.require_session_volume"):
        pytest.skip("liquidity.require_session_volume is off in this config")
    rows = orderly_rows()
    rows[2] = clean_row(100.0, "C", 0.34, volume=None)

    smile = Smile.from_rows(rows, spot=100.0, filters=filters)
    assert smile is not None
    assert 100.0 not in smile.strikes
    assert smile.hygiene.dropped == {DROP_NO_VOLUME: 1}


def test_a_strike_outside_the_delta_band_is_dropped(config, filters):
    """A 0.02-delta wing has almost no vega: its implied vol is a rounding
    artefact of a price quoted in cents, not a measurement."""
    from obliskiq.compute.smile import DROP_DELTA_BAND

    floor = config.require_float("smile.min_abs_delta")
    rows = orderly_rows()
    rows[4] = clean_row(120.0, "C", 0.40, delta=floor / 2)

    smile = Smile.from_rows(rows, spot=100.0, filters=filters)
    assert smile is not None
    assert 120.0 not in smile.strikes
    assert smile.hygiene.dropped == {DROP_DELTA_BAND: 1}


def test_a_missing_delta_is_not_a_failure(filters):
    """Absence is the route not serving greeks, which is a different thing from
    a strike being outside the band. Rejecting on it would empty the smile on
    every route that carries no delta."""
    smile = Smile.from_rows(orderly_rows(delta=None), spot=100.0, filters=filters)
    assert smile is not None
    assert len(smile) == 5
    assert smile.hygiene.dropped == {}


def test_a_bad_print_off_the_chord_of_its_neighbours_is_dropped(config, filters):
    """The butterfly check, in IV space. One strike printing 0.34 where its
    neighbours say 0.36 and 0.36 is a smile; one printing 0.90 is a bad tick,
    and it is the tick that would drive N(d2)."""
    from obliskiq.compute.smile import DROP_NON_CONVEX

    tolerance = config.require_float("smile.butterfly_max_iv_deviation")
    rows = orderly_rows()
    rows[2] = clean_row(100.0, "C", 0.34 + tolerance * 3)

    smile = Smile.from_rows(rows, spot=100.0, filters=filters)
    assert smile is not None
    assert 100.0 not in smile.strikes
    assert smile.hygiene.dropped == {DROP_NON_CONVEX: 1}


def test_a_smooth_smile_is_not_mistaken_for_a_bad_print(filters):
    """The convexity test must not eat a real skew. A monotone smile with a
    genuine slope is exactly what this module exists to preserve."""
    rows = [
        clean_row(strike, "C" if strike >= 100.0 else "P", iv)
        for strike, iv in ((80.0, 0.50), (90.0, 0.44), (100.0, 0.38), (110.0, 0.34), (120.0, 0.32))
    ]
    smile = Smile.from_rows(rows, spot=100.0, filters=filters)
    assert smile is not None
    assert len(smile) == 5
    assert smile.hygiene.dropped == {}


def test_a_chain_that_fails_hygiene_entirely_is_absent_not_unfiltered(config, filters):
    """The decision that matters. There is no laxer second pass: two standards
    behind one implied vol is how a number nobody can defend reaches a table.
    Unmeasurable is the right answer when the chain cannot support the
    measurement."""
    floor = config.require_int("liquidity.min_open_interest")
    rows = orderly_rows(open_interest=floor - 1)

    assert Smile.from_rows(rows, spot=100.0, filters=filters) is None
    # ... and the same rows unfiltered still build, which is what proves the
    # absence came from hygiene rather than from the rows being unusable.
    assert Smile.from_rows(rows, spot=100.0) is not None


def test_a_smile_below_the_minimum_point_count_is_absent(config, filters):
    """Below smile.min_points there is no smile, only points."""
    minimum = config.require_int("smile.min_points")
    rows = orderly_rows()[: minimum - 1]

    assert Smile.from_rows(rows, spot=100.0, filters=filters) is None


def test_the_smile_carries_the_market_data_state_of_its_rows(filters):
    """SKILL.md: label anything FROZEN or DELAYED as indicative in every table
    it appears in. The store has recorded this per row since migration 006 and
    nothing read it."""
    from obliskiq.compute.frames import QUALITY_INDICATIVE, QUALITY_LIVE

    live = Smile.from_rows(orderly_rows(), spot=100.0, filters=filters)
    assert live is not None
    assert live.market_data_quality == QUALITY_LIVE

    rows = orderly_rows()
    rows[2] = clean_row(100.0, "C", 0.34, market_data_status="FROZEN")
    mixed = Smile.from_rows(rows, spot=100.0, filters=filters)
    assert mixed is not None
    assert mixed.market_data_quality == QUALITY_INDICATIVE, (
        "one frozen quote among four live ones makes the table indicative; never mix "
        "frozen and live quotes unmarked"
    )
    assert mixed.market_data_statuses == {"REALTIME": 4, "FROZEN": 1}


def test_an_unreported_status_is_its_own_answer_not_a_claim_of_freshness(filters):
    """Migration 006: NULL means the route said nothing, never that it said
    realtime. The vendor route writes NULL by design."""
    from obliskiq.compute.frames import QUALITY_UNREPORTED

    smile = Smile.from_rows(orderly_rows(market_data_status=None), spot=100.0, filters=filters)
    assert smile is not None
    assert smile.market_data_quality == QUALITY_UNREPORTED
    assert smile.market_data_statuses == {}
