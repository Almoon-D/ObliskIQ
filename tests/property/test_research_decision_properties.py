"""The decision rule over generated intervals: every interval lands in exactly one outcome
(DECISIONS D62), and its labels follow the interval alone.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from hypothesis import given
from hypothesis import strategies as st

from obliskiq.research.decide import AdequacyCheck, Label, Outcome, decide

ends = st.floats(min_value=-1.0, max_value=1.0, allow_nan=False)
thetas = st.floats(min_value=1e-6, max_value=0.5, allow_nan=False)
powers = st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0))
PASSED = (AdequacyCheck("coverage", True, "ok"),)


@given(first=ends, second=ends, theta=thetas, power=powers)
def test_every_interval_lands_in_exactly_one_outcome(first, second, theta, power):
    low, high = min(first, second), max(first, second)
    decision = decide(
        interval=(low, high), theta=theta, power=power, target_power=0.8, checks=PASSED
    )
    memberships = {
        Outcome.SUFFICIENT: low >= theta,
        Outcome.INSUFFICIENT: high < theta,
        Outcome.INCONCLUSIVE: low < theta <= high,
    }
    assert sum(memberships.values()) == 1
    assert memberships[decision.outcome]
    assert (Label.NEGATIVE in decision.labels) == (
        decision.outcome is Outcome.INSUFFICIENT and high < 0
    )
    assert (Label.POSITIVE in decision.labels) == (
        decision.outcome is Outcome.INCONCLUSIVE and low > 0
    )
    assert (Label.SIGN_UNCERTAIN in decision.labels) == (
        decision.outcome is Outcome.INCONCLUSIVE and low <= 0
    )
    assert (Label.LOW_POWER in decision.labels) == (power is not None and power < 0.8)
    assert decision.cannot_conclude


@given(first=ends, second=ends, theta=thetas)
def test_a_failed_check_is_never_evaluated(first, second, theta):
    low, high = min(first, second), max(first, second)
    failed = (AdequacyCheck("coverage", False, "a year without prices"),)
    decision = decide(interval=(low, high), theta=theta, power=0.9, target_power=0.8, checks=failed)
    assert decision.outcome is Outcome.NOT_EVALUABLE
    assert decision.labels == ()
