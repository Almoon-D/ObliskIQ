"""The risk engine's equity side (lite's ``tests/test_compute_risk.py``, its stop and
averaging-down half, ported): volatility-scaled stops and the trailing stop, the averaging-down
rule and the pre-earnings decision.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import date, timedelta

import pytest

from obliskiq.config import ObliskConfig
from obliskiq.domain.market import Bar
from obliskiq.engines.risk.rules import EarningsAction, EntryRules, Reunderwrite
from obliskiq.engines.risk.stops import StopEngine

OPENED = date(2026, 6, 1)
AS_OF = date(2026, 10, 22)


def bars(make_bar: Callable[..., Bar], *, swing: float, count: int = 30) -> list[Bar]:
    """Bars with a constant true range of ``2 * swing`` around a flat close of 100."""
    return [
        make_bar(
            session_date=AS_OF - timedelta(days=count - i),
            open=100.0,
            high=100.0 + swing,
            low=100.0 - swing,
            close=100.0,
        )
        for i in range(count)
    ]


@pytest.fixture
def stops(shipped_settings: ObliskConfig) -> StopEngine:
    return StopEngine(shipped_settings)


def test_the_stop_is_the_configured_atr_multiple_and_nothing_else(
    shipped_settings: ObliskConfig, stops: StopEngine, make_bar: Callable[..., Bar]
) -> None:
    risk = shipped_settings.equity_risk
    levels = stops.stops(bars(make_bar, swing=1.0), entry_price=100.0)
    assert levels is not None
    assert levels.atr == pytest.approx(2.0)
    assert levels.stop == pytest.approx(100.0 - risk.stop_atr_multiple * 2.0)
    assert levels.atr_period == risk.atr_period
    assert stops.stop_type == "volatility_scaled"


def test_the_stop_widens_for_a_volatile_name(
    stops: StopEngine, make_bar: Callable[..., Bar]
) -> None:
    quiet = stops.stops(bars(make_bar, swing=0.5), entry_price=100.0)
    loud = stops.stops(bars(make_bar, swing=4.0), entry_price=100.0)
    assert quiet is not None
    assert loud is not None
    assert loud.stop_distance_pct > quiet.stop_distance_pct
    assert loud.stop_distance_pct != quiet.stop_distance_pct, "no two names share a fixed percent"


def test_a_gap_widens_the_true_range(stops: StopEngine, make_bar: Callable[..., Bar]) -> None:
    flat = bars(make_bar, swing=1.0)
    gapped = [
        *flat[:-1],
        flat[-1].model_copy(update={"open": 90.0, "high": 91.0, "low": 89.0, "close": 90.0}),
    ]
    calm = stops.atr(flat)
    wide = stops.atr(gapped)
    assert calm is not None
    assert wide is not None
    assert wide > calm


def test_the_trailing_stop_follows_the_running_high(
    shipped_settings: ObliskConfig, stops: StopEngine, make_bar: Callable[..., Bar]
) -> None:
    risk = shipped_settings.equity_risk
    levels = stops.stops(bars(make_bar, swing=1.0), entry_price=100.0, trailing_high=130.0)
    assert levels is not None
    assert levels.trailing_stop == pytest.approx(130.0 - risk.trailing_stop_atr_multiple * 2.0)
    assert levels.effective_stop == levels.trailing_stop
    plain = stops.stops(bars(make_bar, swing=1.0), entry_price=100.0)
    assert plain is not None
    assert plain.effective_stop == plain.stop


def test_the_trailing_stop_is_off_when_configuration_turns_it_off(
    shipped_settings: ObliskConfig, make_bar: Callable[..., Bar]
) -> None:
    risk = shipped_settings.equity_risk.model_copy(update={"trailing_stop_enabled": False})
    engine = StopEngine(shipped_settings.model_copy(update={"equity_risk": risk}))
    levels = engine.stops(bars(make_bar, swing=1.0), entry_price=100.0, trailing_high=130.0)
    assert levels is not None
    assert levels.trailing_stop is None


def test_short_history_gives_no_stop_rather_than_a_guessed_one(
    shipped_settings: ObliskConfig, stops: StopEngine, make_bar: Callable[..., Bar]
) -> None:
    few = bars(make_bar, swing=1.0, count=shipped_settings.equity_risk.atr_period)
    assert stops.stops(few, entry_price=100.0) is None
    assert stops.stops(bars(make_bar, swing=1.0), entry_price=0.0) is None


def test_a_bar_with_no_high_is_skipped_not_filled(
    stops: StopEngine, make_bar: Callable[..., Bar]
) -> None:
    rows = bars(make_bar, swing=1.0, count=40)
    rows[-5] = rows[-5].model_copy(update={"high": None})
    atr = stops.atr(rows)
    assert atr == pytest.approx(2.0)


# ── averaging down ──


@pytest.fixture
def rules(shipped_settings: ObliskConfig) -> EntryRules:
    return EntryRules(shipped_settings)


def test_averaging_down_is_refused_without_a_reunderwrite(rules: EntryRules) -> None:
    verdict = rules.averaging_down(opened_on=OPENED, reunderwrites=(), as_of=AS_OF)
    assert not verdict.allowed
    assert "falling price is not a catalyst" in verdict.reason


def test_a_new_dated_catalyst_still_ahead_permits_it(rules: EntryRules) -> None:
    log = (Reunderwrite(AS_OF - timedelta(days=2), "FDA decision", AS_OF + timedelta(days=30)),)
    verdict = rules.averaging_down(opened_on=OPENED, reunderwrites=log, as_of=AS_OF)
    assert verdict.allowed
    assert verdict.catalyst_dates == (AS_OF + timedelta(days=30),)


@pytest.mark.parametrize(
    "entry",
    [
        Reunderwrite(AS_OF - timedelta(days=40), "earnings", AS_OF - timedelta(days=1)),  # passed
        Reunderwrite(AS_OF - timedelta(days=2), "the same catalyst", OPENED),  # not new
        Reunderwrite(AS_OF - timedelta(days=2), "  ", AS_OF + timedelta(days=9)),  # undated words
        Reunderwrite(AS_OF - timedelta(days=2), "a hope", None),  # undated
        Reunderwrite(AS_OF + timedelta(days=1), "logged later", AS_OF + timedelta(days=9)),
    ],
)
def test_a_catalyst_that_is_passed_old_undated_or_not_yet_logged_does_not(
    rules: EntryRules, entry: Reunderwrite
) -> None:
    assert not rules.averaging_down(opened_on=OPENED, reunderwrites=(entry,), as_of=AS_OF).allowed


def test_configuration_may_allow_averaging_down(shipped_settings: ObliskConfig) -> None:
    risk = shipped_settings.equity_risk.model_copy(update={"allow_averaging_down": True})
    lax = EntryRules(shipped_settings.model_copy(update={"equity_risk": risk}))
    assert lax.averaging_down(opened_on=OPENED, reunderwrites=(), as_of=AS_OF).allowed


# ── the pre-earnings rule ──


def test_no_report_in_the_window_needs_no_decision(rules: EntryRules) -> None:
    found = rules.pre_earnings(
        as_of=AS_OF,
        hold_until=AS_OF + timedelta(days=20),
        reports=[AS_OF + timedelta(days=40), AS_OF - timedelta(days=5)],
        entry_range_pctile=0.95,
    )
    assert found.action is EarningsAction.NONE


def test_uncollected_dates_are_unknown_never_clear(rules: EntryRules) -> None:
    found = rules.pre_earnings(
        as_of=AS_OF, hold_until=AS_OF + timedelta(days=20), reports=None, entry_range_pctile=0.5
    )
    assert found.action is EarningsAction.UNKNOWN


def test_a_report_flags_and_sizes_and_above_the_trigger_needs_a_decision(
    shipped_settings: ObliskConfig, rules: EntryRules
) -> None:
    trigger = shipped_settings.equity_risk.pre_earnings_range_decile_trigger
    report = AS_OF + timedelta(days=10)
    kwargs = {"as_of": AS_OF, "hold_until": AS_OF + timedelta(days=60), "reports": [report]}
    low = rules.pre_earnings(entry_range_pctile=trigger - 0.1, **kwargs)  # type: ignore[arg-type]
    high = rules.pre_earnings(entry_range_pctile=trigger, **kwargs)  # type: ignore[arg-type]
    unmeasured = rules.pre_earnings(entry_range_pctile=None, **kwargs)  # type: ignore[arg-type]
    assert low.action is EarningsAction.FLAG_AND_SIZE
    assert low.report_on == report
    assert high.action is EarningsAction.DECIDE
    assert "reduce, hedge" in high.reason
    assert unmeasured.action is EarningsAction.FLAG_AND_SIZE
    assert "untested" in unmeasured.reason


@pytest.mark.parametrize(
    ("rule", "action"),
    [("hold", EarningsAction.HOLD), ("force_reduce", EarningsAction.FORCE_REDUCE)],
)
def test_the_configured_rule_decides(
    shipped_settings: ObliskConfig, rule: str, action: EarningsAction
) -> None:
    risk = shipped_settings.equity_risk.model_copy(update={"pre_earnings_rule": rule})
    engine = EntryRules(shipped_settings.model_copy(update={"equity_risk": risk}))
    found = engine.pre_earnings(
        as_of=AS_OF,
        hold_until=AS_OF + timedelta(days=60),
        reports=[AS_OF + timedelta(days=10)],
        entry_range_pctile=0.99,
    )
    assert found.action is action
