"""The trend tilt's signals by hand: momentum over cash, the moving-average gap, hysteresis and the
scale with its floor.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import random

import pytest

from obliskiq.quant.trend import TrendRule, hysteretic, trend_state

RULE = TrendRule(momentum_months=3, average_months=2, hysteresis=0.02, floor=0.25)


def replayed(measures: list[float], buffer: float) -> bool:
    """The state machine stepped through one measure at a time (the definition)."""
    state = measures[0] >= 0
    for measure in measures[1:]:
        if measure > buffer:
            state = True
        elif measure < -buffer:
            state = False
    return state


def test_hysteresis_holds_inside_the_buffer_and_turns_outside_it():
    assert hysteretic([0.0], 0.02) is True  # zero is positive
    assert hysteretic([-0.01, 0.015], 0.02) is False  # inside the buffer: still negative
    assert hysteretic([-0.01, 0.03], 0.02) is True
    assert hysteretic([0.05, -0.03, 0.01, -0.019], 0.02) is False
    with pytest.raises(ValueError, match="at least one measure"):
        hysteretic([], 0.02)


def test_the_vectorised_state_is_the_state_machine():
    rng = random.Random(4)
    for _ in range(2000):
        measures = [rng.choice([0.0, 0.02, -0.02, rng.uniform(-0.06, 0.06)]) for _ in range(9)]
        assert hysteretic(measures, 0.02) is replayed(measures, 0.02), measures


def test_the_scale_by_hand():
    # Four month ends; momentum over three months against cash; a two-month average.
    closes = [100.0, 104.0, 106.0, 110.0]
    cash = [100.0, 100.0, 100.0, 101.0]
    state = trend_state(closes, cash, RULE)
    assert state is not None
    # 110 / 100 - 101 / 100 = 0.09: positive. Gaps: 104/102 - 1, 106/105 - 1, 110/108 - 1.
    assert state.momentum == pytest.approx(0.09)
    assert state.average_gap == pytest.approx(110 / 108 - 1)
    assert (state.momentum_positive, state.average_positive, state.scale) == (True, True, 1.0)
    falling = trend_state([100.0, 98.0, 95.0, 90.0], cash, RULE)
    assert falling is not None
    assert falling.scale == pytest.approx(0.25)  # both negative: the floor, never zero
    mixed = trend_state([100.0, 120.0, 90.0, 102.0], cash, RULE)
    assert mixed is not None
    # Momentum 102/100 - 1.01 = 0.01, inside the buffer at its only evaluation: positive by
    # sign. The gap ends at 102/96 - 1 > 0.02 after a dip below: positive too.
    assert (mixed.momentum_positive, mixed.average_positive) == (True, True)
    half = trend_state([100.0, 101.0, 102.0, 98.5], [100.0, 100.0, 100.0, 100.0], RULE)
    assert half is not None
    # Momentum -0.015 at its one evaluation (sign negative); the gap 98.5/100.25 - 1 = -0.0175
    # is inside the buffer after positive gaps: still positive. One of two signals is positive.
    assert (half.momentum_positive, half.average_positive) == (False, True)
    assert half.scale == pytest.approx(0.25 + 0.75 / 2)


def test_a_short_or_bad_history_has_no_state():
    assert RULE.months_needed == 4
    assert trend_state([100.0, 101.0, 102.0], [1.0, 1.0, 1.0], RULE) is None
    with pytest.raises(ValueError, match="align"):
        trend_state([100.0, 101.0], [1.0], RULE)
    with pytest.raises(ValueError, match="positive"):
        trend_state([100.0, 0.0, 1.0, 2.0], [1.0] * 4, RULE)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"momentum_months": 0}, "momentum needs one month"),
        ({"average_months": 1}, "the average two or more"),
        ({"hysteresis": 1.0}, "hysteresis"),
        ({"floor": 0.0}, "floor"),
    ],
)
def test_a_rule_out_of_range_is_refused(fields, message):
    values = {"momentum_months": 12, "average_months": 10, "hysteresis": 0.02, "floor": 0.25}
    with pytest.raises(ValueError, match=message):
        TrendRule(**{**values, **fields})
