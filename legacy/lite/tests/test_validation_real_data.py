"""Validation against REAL collected data, not fixtures written beside the code.

Every other test in this suite checks that an engine agrees with a fixture built
alongside it. That proves determinism, not meaning. The values asserted here were
recomputed BY HAND from ``tests/fixtures/mcp_capture/raw`` — Session 7's live
IBKR capture of ASML — with an independent implementation written from the
formula rather than by calling the shipped function. Where the two disagree, the
disagreement is the finding.

Two external anchors are used, both served by IBKR alongside the bars:

* ``historical-vol`` gives ``daily_pct`` and ``annual_pct`` for the same
  instrument. Their ratio squared pins the annualisation basis to 252 exactly,
  independently of anything in config.yaml.
* ``implied-vol`` does the same for an option contract.

The capture is deliberately short — 40 daily bars over one underlying on one
day. That is not a limitation to work around: it is the case the engines must
refuse correctly, so the refusals are asserted here as carefully as the values.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pytest

from obliskiq.collect.vol import (
    garman_klass_vol,
    ohlc_bars,
    parkinson_vol,
    realised_vol,
    restated_open_share,
)
from obliskiq.compute.stats import correlation, paired_log_returns, percentile_of

RAW = Path(__file__).resolve().parent / "fixtures" / "mcp_capture" / "raw"
ASML_CONID = 117589399

#: IBKR's own figures for ASML, from snapshot_117589399.json in the capture.
VENDOR_HV_DAILY = 0.0340083
VENDOR_HV_ANNUAL = 0.5398650258724675
#: From snapshot_683450790.json, an ASML DEC26 call.
VENDOR_IV_DAILY = 0.030435022450729664
VENDOR_IV_ANNUAL = 0.4831410033077896


def _closes() -> list[float]:
    body = json.loads((RAW / f"history_{ASML_CONID}_1d.json").read_text())
    return [float(c) for c in body["close"]]


def _ohlc() -> tuple[list[float], list[float], list[float], list[float]]:
    """Open, high, low and close from the same capture.

    The capture carries all four — this is real IBKR OHLC for a real instrument,
    which is what makes the range estimators checkable against something other
    than a fixture written beside them.
    """
    body = json.loads((RAW / f"history_{ASML_CONID}_1d.json").read_text())
    return (
        [float(v) for v in body["open"]],
        [float(v) for v in body["high"]],
        [float(v) for v in body["low"]],
        [float(v) for v in body["close"]],
    )


def _dates() -> list[str]:
    body = json.loads((RAW / f"history_{ASML_CONID}_1d.json").read_text())
    return [str(t)[:10] for t in body["time"]]


def _hand_realised_vol(closes: list[float], window: int, basis: float = 252.0) -> float:
    """Realised vol from the formula, not from the shipped function.

    Log returns, sample standard deviation about the sample mean (n-1),
    annualised by the square root of the basis.
    """
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))][-window:]
    n = len(returns)
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    return math.sqrt(variance * basis)


def _hand_parkinson_vol(
    highs: list[float], lows: list[float], window: int, basis: float = 252.0
) -> float:
    """Parkinson from the formula, not from the shipped function.

    ``sqrt( sum (ln H/L)^2 / (4 ln2 * n) * basis )``. Written out with its own
    literal 4 and ln 2 rather than importing the module's constants: a hand
    computation that borrows the shipped scale factor would agree with a wrong
    one.
    """
    bars = list(zip(highs, lows, strict=True))[-window:]
    total = sum(math.log(high / low) ** 2 for high, low in bars)
    return math.sqrt(total / (4.0 * math.log(2.0) * len(bars)) * basis)


def _hand_garman_klass_vol(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    window: int,
    basis: float = 252.0,
) -> float:
    """Garman-Klass from the formula: ``0.5 (ln H/L)^2 - (2 ln2 - 1)(ln C/O)^2``."""
    bars = list(zip(opens, highs, lows, closes, strict=True))[-window:]
    total = 0.0
    for open_, high, low, close in bars:
        total += 0.5 * math.log(high / low) ** 2
        total -= (2.0 * math.log(2.0) - 1.0) * math.log(close / open_) ** 2
    return math.sqrt(total / len(bars) * basis)


# ── the external anchor ───────────────────────────────────────────────────────


def test_the_annualisation_basis_is_252_by_ibkrs_own_arithmetic():
    """IBKR serves daily AND annual vol. Their ratio squared IS the basis.

    This is the one check in the suite that does not trust config.yaml: if IBKR
    ever moved to 260 or 365, this fails without anything in the repo changing.
    """
    assert pytest.approx(252.0, abs=1e-6) == (VENDOR_HV_ANNUAL / VENDOR_HV_DAILY) ** 2
    assert pytest.approx(252.0, abs=1e-6) == (VENDOR_IV_ANNUAL / VENDOR_IV_DAILY) ** 2


def test_the_shipped_annualisation_matches_the_vendors(config):
    """config.yaml's basis and IBKR's are the same number."""
    assert config.require_float("data.trading_days_per_year") == pytest.approx(252.0)


# ── the engine against the hand computation ───────────────────────────────────


@pytest.mark.parametrize("tenor", [10, 30])
def test_realised_vol_matches_an_independent_hand_computation(tenor):
    closes = _closes()
    shipped = realised_vol(closes, tenor_days=tenor, trading_days_per_year=252.0)
    assert shipped == pytest.approx(_hand_realised_vol(closes, tenor), rel=1e-12)


def test_the_hand_computed_values_are_the_ones_recorded_in_the_session_report():
    """Pin the actual numbers, so a change of convention is visible as a diff."""
    closes = _closes()
    assert _hand_realised_vol(closes, 10) == pytest.approx(0.393534, abs=5e-6)
    assert _hand_realised_vol(closes, 30) == pytest.approx(0.462331, abs=5e-6)


@pytest.mark.parametrize("tenor", [10, 30])
def test_parkinson_matches_an_independent_hand_computation(tenor):
    _, highs, lows, _ = _ohlc()
    shipped = parkinson_vol(highs, lows, tenor_days=tenor, trading_days_per_year=252.0)
    assert shipped == pytest.approx(_hand_parkinson_vol(highs, lows, tenor), rel=1e-12)


@pytest.mark.parametrize("tenor", [10, 30])
def test_garman_klass_matches_an_independent_hand_computation(tenor):
    opens, highs, lows, closes = _ohlc()
    shipped = garman_klass_vol(
        opens, highs, lows, closes, tenor_days=tenor, trading_days_per_year=252.0
    )
    assert shipped == pytest.approx(
        _hand_garman_klass_vol(opens, highs, lows, closes, tenor), rel=1e-12
    )


def test_the_hand_computed_range_values_are_the_ones_recorded_in_the_session_report():
    """Pin the actual numbers, so a change of convention is visible as a diff."""
    opens, highs, lows, closes = _ohlc()
    assert _hand_parkinson_vol(highs, lows, 10) == pytest.approx(0.299125, abs=5e-6)
    assert _hand_parkinson_vol(highs, lows, 30) == pytest.approx(0.433596, abs=5e-6)
    assert _hand_garman_klass_vol(opens, highs, lows, closes, 10) == pytest.approx(
        0.290386, abs=5e-6
    )
    assert _hand_garman_klass_vol(opens, highs, lows, closes, 30) == pytest.approx(
        0.430298, abs=5e-6
    )


def test_the_range_estimators_read_below_close_to_close_because_they_miss_the_gap(config):
    """The bias that decides the whole design, measured on real bars.

    Parkinson and Garman-Klass both assume the price diffuses continuously
    through the session, so both measure the INTRADAY range and see nothing of
    the overnight move. On this capture the mean absolute overnight log move is
    roughly four fifths of the mean absolute intraday one — a large fraction of
    the daily move that neither estimator can see.

    So they read BELOW the close-to-close figure, and always will on a market
    that closes at night. That is a bias rather than noise, it does not average
    away, and it is why the two are additional leaves rather than a replacement,
    why the ranked view orders on the percentile and never the level, and why
    ``iv_minus_hv`` keeps the close-to-close figure — an implied vol prices the
    total variance to expiry, gaps included.
    """
    opens, highs, lows, closes = _ohlc()

    gaps = [abs(math.log(opens[i] / closes[i - 1])) for i in range(1, len(closes))]
    intraday = [abs(math.log(closes[i] / opens[i])) for i in range(1, len(closes))]
    mean_gap = sum(gaps) / len(gaps)
    mean_intraday = sum(intraday) / len(intraday)
    assert mean_gap == pytest.approx(0.01602, abs=5e-5)
    assert mean_intraday == pytest.approx(0.01937, abs=5e-5)
    assert mean_gap > mean_intraday / 2, "the overnight move is not a rounding error here"

    close_to_close = _hand_realised_vol(closes, 30)
    assert _hand_parkinson_vol(highs, lows, 30) < close_to_close
    assert _hand_garman_klass_vol(opens, highs, lows, closes, 30) < close_to_close

    # Not a swap, and this is the arithmetic reason as well as the recorded one.
    assert config.require_int("data.strike_bounds_rv_tenor_days") == 30


def test_the_asml_capture_carries_a_genuine_open_so_garman_klass_is_admitted(config):
    """Check what the fixtures CONTAIN before assuming an open is real.

    Not one of the 39 gaps in this capture opens at exactly the previous close,
    so ASML publishes a real opening print and Garman-Klass may read it. The
    opposite case — a venue that restates the close — is the one the refusal
    exists for, and no fixture in this repository exhibits it; see
    ``references/ibkr-playbook.md``, where it is recorded as UNVERIFIED on this
    route rather than as an observation.
    """
    opens, highs, lows, closes = _ohlc()
    bars = ohlc_bars(opens, highs, lows, closes)
    assert len(bars) == 40, "every captured bar is usable: none is missing a price"

    share = restated_open_share(bars)
    assert share == 0.0
    assert share <= config.require_float("data.garman_klass_max_restated_open_share")


def test_no_estimator_reproduces_the_vendor_figure_either(config):
    """The Session 8 finding, widened to the two new estimators.

    ``underlying_snapshots.historical_vol_30d`` is IBKR's own number, and the
    earlier test shows no close-to-close window over these bars comes near it.
    Neither does Parkinson nor Garman-Klass — both are further away than the
    closest close-to-close window, not nearer. So adding range estimators does
    not quietly turn the vendor figure into something reproducible, and
    ``hv_source`` is still recording a real distinction.
    """
    opens, highs, lows, closes = _ohlc()
    windows = range(5, 40)
    close_to_close = min(abs(_hand_realised_vol(closes, w) - VENDOR_HV_ANNUAL) for w in windows)
    parkinson = min(abs(_hand_parkinson_vol(highs, lows, w) - VENDOR_HV_ANNUAL) for w in windows)
    garman_klass = min(
        abs(_hand_garman_klass_vol(opens, highs, lows, closes, w) - VENDOR_HV_ANNUAL)
        for w in windows
    )

    assert parkinson > 0.03 and garman_klass > 0.03
    assert parkinson > close_to_close and garman_klass > close_to_close


def test_the_range_estimators_refuse_a_tenor_the_history_cannot_cover():
    """40 bars cannot make a 90-day range estimate. Both must decline.

    Note the one-bar asymmetry with close-to-close, which needs ``tenor + 1``
    PRICES for ``tenor`` returns: a range estimator reads one term per bar, so
    all 40 bars are usable and the refusal is on the tenor alone.
    """
    opens, highs, lows, closes = _ohlc()
    assert len(closes) == 40
    assert parkinson_vol(highs, lows, tenor_days=90, trading_days_per_year=252.0) is None
    assert (
        garman_klass_vol(opens, highs, lows, closes, tenor_days=90, trading_days_per_year=252.0)
        is None
    )
    assert parkinson_vol(highs, lows, tenor_days=40, trading_days_per_year=252.0) is not None


def test_realised_vol_refuses_a_tenor_the_history_cannot_cover():
    """39 returns cannot make a 90-day vol. It must decline, not extrapolate."""
    closes = _closes()
    assert len(closes) == 40
    assert realised_vol(closes, tenor_days=90, trading_days_per_year=252.0) is None


def test_the_vendors_historical_vol_is_not_reproducible_from_the_bars_collected():
    """A finding, pinned as a test so it cannot be forgotten.

    ``underlying_snapshots.historical_vol_30d`` stores IBKR's number under a name
    asserting a 30-day window. No window over the bars this system collects for
    the same instrument on the same day reproduces it — the closest is 3.7 vol
    points away and the nominal 30-day window is 7.8 points away. The two are
    therefore not interchangeable, which is what ``hv_source`` exists to record.
    """
    closes = _closes()
    by_window = {w: _hand_realised_vol(closes, w) for w in range(5, 40)}
    closest = min(by_window.values(), key=lambda v: abs(v - VENDOR_HV_ANNUAL))
    assert abs(closest - VENDOR_HV_ANNUAL) > 0.03, (
        "a window now reproduces the vendor figure; the divergence finding in "
        "docs/archive/SESSION-8-VALIDATION.md needs revisiting"
    )
    assert abs(by_window[30] - VENDOR_HV_ANNUAL) == pytest.approx(0.0775, abs=5e-4)


def test_the_capture_ends_on_a_partial_bar_and_that_moves_the_number():
    """The last bar was taken mid-session, and including it is not free.

    Volume on the final bar is an order of magnitude below the series median
    because the capture ran at 09:32Z. Recording the effect keeps a partial bar
    from being mistaken for a quiet day.
    """
    body = json.loads((RAW / f"history_{ASML_CONID}_1d.json").read_text())
    volumes = [float(v) for v in body["volume"]]
    median = sorted(volumes[:-1])[len(volumes[:-1]) // 2]
    assert volumes[-1] < median / 5

    closes = _closes()
    with_partial = _hand_realised_vol(closes, 39)
    without_partial = _hand_realised_vol(closes[:-1], 38)
    assert abs(with_partial - without_partial) == pytest.approx(0.0065, abs=1e-3)


# ── percentiles: the window actually used ─────────────────────────────────────


def test_percentile_of_is_the_share_at_or_below_and_counts_the_value_itself():
    """Recomputed by hand: ties count as below, and the value is in the sample."""
    sample = [1.0, 2.0, 3.0, 4.0]
    assert percentile_of(sample, 3.0) == pytest.approx(3 / 4)
    assert percentile_of(sample, 1.0) == pytest.approx(1 / 4)
    # A single observation is always its own 100th percentile. This is why
    # grid.min_observations.percentile exists and must be applied by callers.
    assert percentile_of([5.0], 5.0) == pytest.approx(1.0)


def test_a_percentile_needs_more_observations_than_this_capture_holds(config):
    """52 weeks of IV percentile cannot come from one snapshot, and must not."""
    floor = config.require_int("grid.min_observations.percentile")
    assert floor == 26
    assert len(_closes()) < floor * 2


# ── correlation: on returns, on the sessions both series traded ───────────────


def test_correlation_is_computed_on_returns_not_levels():
    """Two series rising together on different paths correlate on returns < 1.

    On LEVELS both are monotone and Pearson would read ~1.0, so this
    distinguishes the two rather than merely asserting a number.
    """
    left = [100.0, 101.0, 103.0, 104.0, 107.0, 108.0]
    right = [50.0, 51.0, 51.2, 52.5, 52.7, 54.0]
    lr, rr = paired_log_returns(left, right)
    on_returns = correlation(lr, rr)
    on_levels = correlation(left, right)
    assert on_returns is not None and on_levels is not None
    # Both series trend up, so the level correlation is high whatever the
    # day-to-day agreement. The return correlation is materially lower, and it
    # is the return figure the grid reports.
    assert on_levels > 0.9
    assert on_returns < on_levels - 0.2


def test_paired_returns_keep_the_two_series_in_lockstep():
    left = [10.0, 11.0, 12.0, 13.0]
    right = [5.0, 5.5, 6.0, 6.5]
    lr, rr = paired_log_returns(left, right)
    assert len(lr) == len(rr) == 3


def test_the_capture_cannot_support_the_longest_correlation_window(config):
    """correlation_250d needs a year of overlap. The capture holds 40 bars."""
    windows = config.require_list("data.correlation_windows_days")
    assert max(int(w) for w in windows) == 250
    assert len(_closes()) < 250


# ── the store the collectors actually wrote ───────────────────────────────────


def test_the_captured_dates_are_trading_sessions_in_order():
    dates = _dates()
    assert dates == sorted(dates)
    assert len(set(dates)) == len(dates)
    assert all(date.fromisoformat(d).weekday() < 5 for d in dates)


# ── the sleeve arithmetic ─────────────────────────────────────────────────────
#
# The inputs here are round numbers chosen so the expected result can be read
# off the line. They are deliberately NOT this account's NAV or any figure from
# a trade it has taken: an assertion pinned to one book's history stops being a
# statement about the engine the moment the book changes, and a machine whose
# tests remember past positions is a machine that reasons from them.

NAV = 10_000.00
LOSS_QUOTE_CCY = 250.00  # a realised option loss, in the contract's currency
QUOTE_PER_BASE = 1.25  # units of that currency per unit of base, ECB convention


def test_the_sleeve_budget_is_a_share_of_current_nav(config):
    """The budget is budget_pct OF NAV, computed rather than stored."""
    pct = config.require_float("option_sleeve.budget_pct")
    assert 0.0 < pct < 1.0
    assert NAV * pct == pytest.approx(500.00, abs=0.005), "5% of 10,000 is 500"


def test_the_penalty_is_the_multiple_of_the_converted_loss(config):
    """A loss converts to base FIRST, and the multiple applies to the base figure.

    Converting after multiplying gives the same number here only because
    multiplication commutes; the order matters for what the ledger stores, and
    penalty.py books a base-currency balance. 250 / 1.25 = 200, twice that = 400.
    """
    multiple = config.require_float("option_sleeve.loss_penalty_multiple")
    assert multiple > 1.0, "a penalty that does not exceed the loss is not a penalty"
    in_base = LOSS_QUOTE_CCY / QUOTE_PER_BASE
    assert in_base == pytest.approx(200.00, abs=0.005)
    assert multiple * in_base == pytest.approx(400.00, abs=0.01)


def test_available_budget_falls_when_nav_falls(store, config):
    """The budget must track NAV, never a figure fixed at any point in time.

    There is no key declaring the basis — option_sleeve.budget_basis was deleted
    in Phase 3 as a value that selected nothing — so the guarantee rests entirely
    on the arithmetic. This is what asserts it.
    """
    from obliskiq.compute.penalty import PenaltyLedger

    ledger = PenaltyLedger(store, config)
    pct = config.require_float("option_sleeve.budget_pct")

    rich = ledger.status(nav=NAV)
    poor = ledger.status(nav=NAV / 2)

    assert rich.gross_budget == pytest.approx(NAV * pct)
    assert poor.gross_budget == pytest.approx(rich.gross_budget / 2)
    assert poor.available_budget < rich.available_budget


def test_a_trailing_stop_gain_reduces_the_penalty_and_an_override_gain_does_not(config):
    """The recovery channel has to include the exit the equity discipline PREFERS.

    SKILL.md on winners: the trailing stop "protects gains without capping upside,
    which is why it is used and mandatory scaling out is not". While
    qualifying_exit_reasons held only [target, time_window], the mandate's own
    recommended way to realise an equity gain could not clear the penalty. A
    single loss can carry a balance most of the way to the sleeve's own size, so
    that came close to a permanent lock imposed by a rule nobody had intended.

    A trailing stop only arms once a position is profitable, so a trailing_stop
    exit is a realised gain by construction; a losing one reduces nothing either
    way, so widening the channel this far does not leak. `override` stays out:
    that is the escape hatch, and an escape hatch that pays is not one.
    """
    qualifying = [
        str(reason) for reason in config.require_list("option_sleeve.qualifying_exit_reasons")
    ]
    assert "trailing_stop" in qualifying
    assert "target" in qualifying
    assert "override" not in qualifying
    allowed = [str(r) for r in config.require_list("discipline.exit_reasons_allowed")]
    assert set(qualifying) <= set(allowed), "a qualifying exit must be an allowed exit"
