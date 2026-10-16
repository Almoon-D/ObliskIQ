"""The decision rule of DECISIONS D62 on named cases, its labels and its refusals.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math

import pytest

from obliskiq.research.decide import AdequacyCheck, Label, Outcome, decide

OK = (AdequacyCheck("point in time", True, "every series as observed"),)
THETA = 0.01


def outcome(low: float, high: float, power: float | None = 0.9):
    return decide(interval=(low, high), theta=THETA, power=power, target_power=0.8, checks=OK)


@pytest.mark.parametrize(
    ("low", "high", "expected", "labels"),
    [
        (0.01, 0.05, Outcome.SUFFICIENT, ()),  # L == theta is sufficient
        (0.02, 0.05, Outcome.SUFFICIENT, ()),
        (-0.03, 0.0099, Outcome.INSUFFICIENT, ()),
        (-0.05, -0.01, Outcome.INSUFFICIENT, (Label.NEGATIVE,)),
        (0.001, 0.05, Outcome.INCONCLUSIVE, (Label.POSITIVE,)),
        (-0.01, 0.05, Outcome.INCONCLUSIVE, (Label.SIGN_UNCERTAIN,)),
        (0.0, 0.01, Outcome.INCONCLUSIVE, (Label.SIGN_UNCERTAIN,)),  # U == theta is inconclusive
    ],
)
def test_each_outcome_and_its_label(low, high, expected, labels):
    decision = outcome(low, high)
    assert (decision.outcome, decision.labels) == (expected, labels)
    assert decision.interval == (low, high)


def test_power_is_a_label_beside_the_outcome_and_low_power_never_reads_as_against():
    low = outcome(-0.01, 0.05, power=0.3)
    assert low.outcome is Outcome.INCONCLUSIVE
    assert low.labels == (Label.SIGN_UNCERTAIN, Label.LOW_POWER)
    assert any("not evidence that the effect is absent" in line for line in low.cannot_conclude)
    unknown = outcome(0.02, 0.05, power=None)
    assert unknown.labels == (Label.POWER_UNKNOWN,)
    assert outcome(-0.05, -0.02, power=0.3).labels == (Label.NEGATIVE, Label.LOW_POWER)


def test_failed_checks_or_no_interval_are_not_evaluable():
    failed = decide(
        interval=(0.02, 0.05),
        theta=THETA,
        power=0.9,
        target_power=0.8,
        checks=(*OK, AdequacyCheck("coverage", False, "2009 has no prices for 3 of 8")),
    )
    assert failed.outcome is Outcome.NOT_EVALUABLE
    assert "Nothing" in failed.cannot_conclude[0]
    missing = decide(interval=None, theta=THETA, power=None, target_power=0.8, checks=OK)
    assert missing.outcome is Outcome.NOT_EVALUABLE


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"theta": 0.0}, "theta is a positive effect"),
        ({"theta": math.nan}, "theta is a positive effect"),
        ({"target_power": 1.0}, "target power"),
        ({"interval": (0.05, 0.01)}, "L <= U"),
        ({"interval": (math.nan, 0.01)}, "two finite ends"),
    ],
)
def test_malformed_inputs_are_refused(kwargs, message):
    arguments = {"interval": (0.0, 0.1), "theta": THETA, "power": 0.9, "target_power": 0.8}
    arguments.update(kwargs)
    with pytest.raises(ValueError, match=message):
        decide(checks=OK, **arguments)
